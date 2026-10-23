"""SRE engine: collect evidence -> rank hypotheses -> propose fixes -> verify.

The rules are deterministic so the diagnosis is explainable and every
hypothesis carries the concrete evidence (log line, metric, probe result)
that supports it. The LLM (if configured) is used on top of this to explain
and to reason about anything the rules don't cover.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import time

import httpx

from . import config
from .liara_client import Limitation, LiaraError, app_url


# ------------------------------------------------------------------ evidence
async def probe(client, app: str, url: str | None = None, n: int = 3) -> dict:
    if getattr(client, "is_mock", False):
        results = [await client.mock_probe(app) for _ in range(n)]
    else:
        url = url or app_url(app)
        results = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            for _ in range(n):
                t = time.perf_counter()
                try:
                    r = await http.get(url)
                    results.append({"status": r.status_code, "latency_ms": int((time.perf_counter() - t) * 1000)})
                except httpx.HTTPError as e:
                    results.append({"status": 0, "latency_ms": int((time.perf_counter() - t) * 1000),
                                    "error": type(e).__name__})
    lat = [r["latency_ms"] for r in results]
    statuses = [r["status"] for r in results]
    return {"url": url or app_url(app), "statuses": statuses, "median_ms": int(statistics.median(lat)),
            "ok": all(200 <= s < 400 for s in statuses),
            "errors": [r.get("error") for r in results if r.get("error")]}


async def _safe(coro, label):
    try:
        return await coro, None
    except Limitation as e:
        return None, f"{label}: {e}"
    except LiaraError as e:
        return None, f"{label}: {e.human()}"
    except Exception as e:  # noqa
        return None, f"{label}: {type(e).__name__}: {e}"


def _ts_seconds(ts) -> float:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return 0.0
    while v > 1e11:  # ns / us / ms -> s
        v /= 1000
    return v


async def collect_evidence(client, app: str, minutes: int = 30, since: float | None = None) -> dict:
    (details, e1), (releases, e2), (logs, e3), (metrics, e4), (probe_r, e5), (events, e6) = await asyncio.gather(
        _safe(client.get_app(app), "وضعیت اپ"),
        _safe(client.list_releases(app, 5), "ریلیزها"),
        _safe(client.logs(app, minutes=minutes), "لاگ‌ها"),
        _safe(client.metrics(app), "متریک‌ها"),
        _safe(probe(client, app), "تست HTTP"),
        _safe(client.events(app, minutes=max(minutes, 120)), "رویدادهای پلتفرم"),
    )
    limits = [e for e in (e1, e2, e3, e4, e5) if e]  # events are optional, not shown as a limitation
    if since and logs:
        logs = [l for l in logs if _ts_seconds(l["ts"]) >= since] or []
    if events and not since:
        logs = (logs or []) + [{"ts": 0, "line": f"[رویداد لیارا] {x}", "releaseId": None} for x in events[-15:]]
    return {"app": app, "details": details or {}, "releases": releases or [], "logs": logs or [],
            "metrics": (metrics or {}).get("metrics", metrics) or {}, "probe": probe_r or {},
            "events": events or [], "limitations": limits, "collected_at": time.strftime("%H:%M:%S")}


# -------------------------------------------------------------------- rules
STACK_RE = re.compile(r"at (?:\S+ )?\(?/(?:usr/src/app/|app/|workspace/|home/\w+/app/)?([\w./-]+\.\w+):(\d+):\d+\)?")
ENV_MISSING_RE = [
    re.compile(r"\b([A-Z][A-Z0-9_]{2,}) is not (?:set|defined)"),
    re.compile(r"[Mm]issing (?:required )?env(?:ironment)? (?:variable)?:? ?['\"]?([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"KeyError: '([A-Z][A-Z0-9_]{2,})'"),
    re.compile(r"ImproperlyConfigured: Set the ([A-Z][A-Z0-9_]{2,})"),
]
OOM_RE = re.compile(r"(heap out of memory|OOMKilled|MemoryError|Killed|out of memory)", re.I)
MODULE_RE = re.compile(r"(Cannot find module '([^']+)'|ModuleNotFoundError: No module named '([^']+)')")
LISTEN_RE = re.compile(r"(?:listening|running|started) on (?:port )?:?(\d{2,5})", re.I)
DB_RE = re.compile(r"(ECONNREFUSED|could not connect to server|Connection refused|ETIMEDOUT|getaddrinfo ENOTFOUND)")
HTTP_LINE_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH) (\S+) (\d{3}) (\d+)ms")


def _lines(ev):
    return [l["line"] for l in ev["logs"]]


def _find(ev, regex, k=3):
    hits = [l for l in _lines(ev) if regex.search(l)]
    return hits[-k:]


def _code_refs(ev) -> list[dict]:
    refs, seen = [], set()
    for l in _lines(ev):
        m = STACK_RE.search(l)
        if m and (m.group(1), m.group(2)) not in seen and "node_modules" not in m.group(1):
            seen.add((m.group(1), m.group(2)))
            f = m.group(1)
            f = "src/" + ("/" + f).split("/src/", 1)[1] if "/src/" in "/" + f else f.rsplit("/", 1)[-1]
            ref = {"file": f, "line": int(m.group(2))}
            if config.GITHUB_REPO:
                ref["url"] = (f"https://github.com/{config.GITHUB_REPO}/blob/{config.GITHUB_BRANCH}/"
                              f"{ref['file']}#L{ref['line']}")
            refs.append(ref)
    return refs[:3]


def _http_stats(ev):
    rows = [HTTP_LINE_RE.search(l) for l in _lines(ev)]
    rows = [r for r in rows if r]
    if not rows:
        return {}
    errors = sum(1 for r in rows if r.group(3).startswith("5"))
    lat = [int(r.group(4)) for r in rows]
    slow = {}
    for r in rows:
        if int(r.group(4)) > 1000:
            slow[r.group(2)] = max(slow.get(r.group(2), 0), int(r.group(4)))
    return {"requests": len(rows), "error_rate": round(errors / len(rows), 2),
            "p50_ms": int(statistics.median(lat)), "max_ms": max(lat), "slow_routes": slow}


def analyze(ev: dict) -> dict:
    hyps: list[dict] = []
    d, m, pr = ev["details"], ev["metrics"] or {}, ev["probe"] or {}
    app = ev["app"]
    stats = _http_stats(ev)
    code = _code_refs(ev)
    mem = m.get("memory") if isinstance(m.get("memory"), (int, float)) else None
    cpu = m.get("cpu") if isinstance(m.get("cpu"), (int, float)) else None

    def add(key, title, confidence, evidence, fixes, explain, permanent=None):
        hyps.append({"key": key, "title": title, "confidence": confidence, "evidence": evidence,
                     "fixes": fixes, "explain": explain, "permanent_fix": permanent, "code": code})

    # 1. app stopped
    if d.get("scale") == 0 or str(d.get("status", "")).upper() in ("STOPPED", "SLEEPING"):
        add("stopped", "سرویس خاموش است", 0.97, [f"وضعیت اپ: {d.get('status')} / scale={d.get('scale')}"],
            [{"tool": "start_app", "args": {"app": app}}], "اپ در حال حاضر اجرا نمی‌شود؛ باید روشن شود.")

    # 2. missing env var
    for l in _lines(ev):
        for rx in ENV_MISSING_RE:
            mm = rx.search(l)
            if mm:
                key = mm.group(1)
                present = {e.get("key") for e in d.get("envs", [])}
                if key not in present:
                    add("missing_env", f"متغیر محیطی ‎{key}‎ تنظیم نشده", 0.93,
                        [f"لاگ: {l.strip()}", f"‎{key}‎ در متغیرهای محیطی اپ وجود ندارد",
                         f"تست HTTP: کدهای {pr.get('statuses')}"],
                        [{"tool": "set_env", "args": {"app": app, "variables": {key: "<مقدار را وارد کنید>"}},
                          "needs_value": key}],
                        f"برنامه برای اجرای درخواست‌ها به ‎{key}‎ نیاز دارد ولی این مقدار روی سرور تعریف نشده؛ "
                        f"به همین دلیل درخواست‌ها با خطای ۵۰۰ برمی‌گردند.")
                break
        if hyps and hyps[-1]["key"] == "missing_env":
            break

    # 3. memory exhaustion / leak
    oom = _find(ev, OOM_RE)
    if oom or (mem is not None and mem >= 85):
        conf = 0.9 if oom and (mem or 0) >= 85 else 0.75 if oom else 0.6
        evid = [f"لاگ: {l.strip()}" for l in oom[:2]]
        if mem is not None:
            evid.append(f"مصرف RAM: ٪{mem} از سقف پلن")
        if m.get("restarts1h"):
            evid.append(f"{m['restarts1h']} بار ری‌استارت ناخواسته در یک ساعت گذشته")
        if pr.get("median_ms"):
            evid.append(f"زمان پاسخ: {pr['median_ms']}ms")
        add("memory", "کمبود حافظه / نشتی حافظه (Memory Leak)", conf, evid,
            [{"tool": "restart_app", "args": {"app": app}, "note": "راه‌حل موقت: حافظه آزاد می‌شود"},
             {"tool": "suggest_plan", "args": {"app": app, "direction": "up"}, "note": "اگر مصرف واقعی بالاست: ارتقای پلن"}],
            "حافظه‌ی برنامه پر شده و Garbage Collector مدام کار می‌کند؛ همین باعث کندی و در نهایت "
            "کشته‌شدن کانتینر (OOM) می‌شود. اگر بعد از ری‌استارت مصرف دوباره بالا رفت، مشکل نشتی در کد است.",
            permanent="در کد، داده‌ای که بدون محدودیت در حافظه انباشته می‌شود (مثلاً کش یا آرایه‌ی سراسری) را محدود کنید."
                      + (f" محتمل‌ترین محل: ‎{code[0]['file']}:{code[0]['line']}‎" if code else ""))

    # 4. CPU saturation
    if (cpu is not None and cpu >= 85) or (stats.get("max_ms", 0) > 5000 and not oom):
        evid = []
        if cpu is not None:
            evid.append(f"مصرف CPU: ٪{cpu}")
        for route, ms in list(stats.get("slow_routes", {}).items())[:3]:
            evid.append(f"مسیر کند: ‎{route}‎ تا {ms}ms")
        if any("504" in l for l in _lines(ev)):
            evid.append("خطای 504 (timeout) در لاگ‌ها")
        add("cpu", "پردازنده (CPU) اشباع شده است", 0.8 if cpu and cpu >= 85 else 0.55, evid,
            [{"tool": "suggest_plan", "args": {"app": app, "direction": "up"}, "note": "ارتقای پلن به CPU بیشتر"}],
            "پردازش سنگین در برخی مسیرها باعث شده سرور نتواند به‌موقع به همه‌ی درخواست‌ها پاسخ دهد.",
            permanent="عملیات سنگین (گزارش‌گیری، پردازش تصویر) را به صف پس‌زمینه یا کش منتقل کنید.")

    # 5. broken latest release
    rel = ev["releases"]
    if rel and str(rel[0].get("state", "")).upper() in ("FAILED", "UNHEALTHY", "CANCELED"):
        good = next((r for r in rel[1:] if str(r.get("state", "")).upper() == "READY"), None)
        evid = [f"آخرین ریلیز ({rel[0].get('tag') or rel[0].get('_id')}) در وضعیت {rel[0].get('state')}"]
        evid += [f"لاگ: {l.strip()}" for l in _find(ev, MODULE_RE, 2)]
        fixes = [{"tool": "rollback", "args": {"app": app, "release_id": good["_id"]}}] if good else []
        add("bad_release", "آخرین نسخه‌ی منتشرشده خراب است", 0.88, evid, fixes,
            "نسخه‌ی جدید بعد از دیپلوی سالم بالا نیامده؛ سریع‌ترین راه برگرداندن نسخه‌ی سالم قبلی است.",
            permanent="ماژول/وابستگی جاافتاده را به package.json یا requirements.txt اضافه کنید و دوباره دیپلوی کنید.")
    elif _find(ev, MODULE_RE):
        mm = MODULE_RE.search(_find(ev, MODULE_RE)[-1])
        add("module", f"وابستگی ‎{mm.group(2) or mm.group(3)}‎ نصب نشده", 0.8,
            [f"لاگ: {_find(ev, MODULE_RE)[-1].strip()}"], [],
            "برنامه به کتابخانه‌ای نیاز دارد که در فایل وابستگی‌ها تعریف نشده.",
            permanent="آن را به فایل وابستگی‌ها اضافه کنید و دوباره دیپلوی کنید.")

    # 6. port mismatch
    listen = [LISTEN_RE.search(l) for l in _lines(ev)]
    ports = {int(x.group(1)) for x in listen if x}
    if ports and d.get("port") and d.get("port") not in ports:
        real = sorted(ports)[-1]
        add("port", "پورت برنامه با پورت تنظیم‌شده در لیارا یکی نیست", 0.9,
            [f"برنامه روی پورت {real} اجرا می‌شود", f"لیارا ترافیک را به پورت {d.get('port')} می‌فرستد"],
            [{"tool": "update_port", "args": {"app": app, "port": real}}],
            "لیارا درخواست‌ها را به پورتی می‌فرستد که برنامه روی آن گوش نمی‌دهد؛ نتیجه خطای 502 است.")

    # 7. database / external dependency
    dbl = _find(ev, DB_RE)
    if dbl:
        add("dependency", "اتصال به دیتابیس یا سرویس خارجی برقرار نیست", 0.7,
            [f"لاگ: {l.strip()}" for l in dbl[:2]], [],
            "برنامه نمی‌تواند به دیتابیس/سرویس وابسته وصل شود. آدرس و وضعیت دیتابیس را بررسی کنید.",
            permanent="مقدار متغیر اتصال (مثلاً DATABASE_URL) و روشن بودن دیتابیس را چک کنید؛ "
                      "اگر دیتابیس در لیارا است، هر دو باید در یک شبکه باشند.")

    # generic 5xx without explanation
    if not hyps and pr and not pr.get("ok"):
        add("unknown_5xx", "سایت خطا برمی‌گرداند ولی علت قطعی در لاگ‌ها نیست", 0.4,
            [f"تست HTTP: {pr.get('statuses')}"] + [f"لاگ: {l['line']}" for l in ev["logs"][-3:]],
            [{"tool": "restart_app", "args": {"app": app}}],
            "شواهد کافی برای علت قطعی نیست؛ پیشنهاد می‌کنم لاگ‌های بیشتری بررسی شود.")

    hyps.sort(key=lambda h: -h["confidence"])
    healthy = pr.get("ok") and not hyps
    summary = {
        "status": "healthy" if healthy else ("degraded" if pr.get("ok") else "down"),
        "probe": pr, "http": stats, "metrics": m, "plan": d.get("planID"),
        "limitations": ev["limitations"],
    }
    return {"summary": summary, "hypotheses": hyps}


async def verify(client, app: str, before: dict, wait_s: float = 0, since: float | None = None) -> dict:
    """Re-collect evidence after a fix and compare with the previous state."""
    if wait_s:
        await asyncio.sleep(wait_s)
    ev = await collect_evidence(client, app, minutes=5, since=None if getattr(client, "is_mock", False) else since)
    after = analyze(ev)
    b_keys = {h["key"] for h in before.get("hypotheses", [])}
    a_keys = {h["key"] for h in after["hypotheses"]}
    resolved = sorted(b_keys - a_keys)
    remaining = sorted(b_keys & a_keys)
    bp, ap = before.get("summary", {}).get("probe", {}), after["summary"]["probe"]
    return {
        "resolved": resolved, "remaining": remaining, "new": sorted(a_keys - b_keys),
        "before": {"statuses": bp.get("statuses"), "median_ms": bp.get("median_ms")},
        "after": {"statuses": ap.get("statuses"), "median_ms": ap.get("median_ms")},
        "fixed": ap.get("ok", False) and not remaining,
        "report": after,
    }
