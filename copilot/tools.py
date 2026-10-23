"""Tool registry shared by the chat agent and the MCP server.

Each tool = schema + risk level + plain-Persian approval text + async implementation.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import analyzer, diagnosis
from .liara_client import Limitation, LiaraError, app_url
from .policy import Risk

Emit = Callable[[dict], Awaitable[None]]


@dataclass
class Ctx:
    client: Any
    emit: Emit
    state: dict  # per-session memory: last diagnosis, last analysis, etc.

    async def step(self, text: str, status: str = "running", detail: str | None = None):
        await self.emit({"type": "step", "text": text, "status": status, "detail": detail})


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    risk: Risk
    fn: Callable[..., Awaitable[Any]]
    approval: Callable[[dict], tuple[str, str, str]] | None = None
    required: tuple = ()
    prepare: Callable[..., Awaitable[dict]] | None = None  # enriches args before the approval card

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": list(self.required)}}}


APP = {"type": "string", "description": "نام اپ در لیارا (app name)"}
REGISTRY: dict[str, Tool] = {}


def tool(name, description, params=None, risk=Risk.READ, required=(), approval=None, prepare=None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, params or {}, risk, fn, approval, tuple(required), prepare)
        return fn
    return deco


SECRETY = ("SECRET", "PASSWORD", "PASS", "TOKEN", "KEY", "PRIVATE")


async def prepare_deploy(ctx: "Ctx", args: dict) -> dict:
    """List every environment variable the project uses so the user can fill them in the approval card.

    All of them are OPTIONAL: the user may leave any empty and set it later ("متغیر X را بگذار ...").
    Sources, in priority order: value the user gave in chat -> project's own .env (pre-filled) ->
    already set on the Liara app (shown as 'already set', not re-sent) -> empty.
    Example values from .env.example are only shown as a hint, never used silently.
    """
    source = args.get("source")
    an = ctx.state.get("analysis")
    if not an or an.get("source") != source:
        an = await asyncio.to_thread(analyzer.analyze, source, await _paid_plans(ctx))
        an["source"] = source
        ctx.state["analysis"] = an
    from pathlib import Path
    dotenv, dotenv_src = analyzer.dotenv_values(Path(an["path"]), with_source=True)
    given = {k: str(v) for k, v in (args.get("envs") or {}).items()
             if v not in (None, "") and not str(v).startswith("<")}
    on_liara: set = set()
    if not args.get("create"):
        try:
            on_liara = {e["key"] for e in await ctx.client.get_envs(args["app"]) if e.get("value") not in (None, "")}
        except Exception:
            pass
    keys = list(dict.fromkeys(list(an.get("env_vars", {})) + list(args.get("envs") or {}) + list(dotenv)))
    envs, meta = {}, {}
    for k in keys:
        info = an.get("env_vars", {}).get(k, {})
        ex, where = info.get("example", ""), info.get("used_at", "")
        hint = f"استفاده در: {where}" if where else ""
        if k in given:
            envs[k], meta[k] = given[k], {"required": False, "state": "given",
                                          "hint": "مقدار گفته‌شده در گفت‌وگو" + (f" · {hint}" if hint else "")}
        elif k in dotenv and dotenv[k]:
            local = re.search(r"(localhost|127\.0\.0\.1|0\.0\.0\.0)", dotenv[k])
            envs[k], meta[k] = dotenv[k], {"required": False, "state": "dotenv",
                                           "hint": f"از فایل ‎{dotenv_src.get(k, '.env')}‎ پروژه (خالی بگذارید = همین مقدار)"
                                                   + (" · ⚠️ به localhost اشاره می‌کند؛ روی لیارا در دسترس نیست و باید آدرس واقعی (مثلاً دیتابیس لیارا) بگذارید"
                                                      if local and not k.upper().endswith(("HOST_BIND", "BIND")) else "")}
        elif k in on_liara:
            envs[k], meta[k] = "<keep>", {"required": False, "state": "on_liara", "on_liara": True,
                                         "hint": "روی لیارا تنظیم شده؛ خالی بگذارید تا تغییر نکند"}
        else:
            if ex and not any(w in k.upper() for w in SECRETY):
                hint += (" · " if hint else "") + f"نمونه: {ex}"
            envs[k], meta[k] = f"<{k}>", {"required": False, "state": "missing",
                                         "hint": (hint or "") + (" · " if hint else "") + "اختیاری؛ می‌توانید بعداً هم بدهید"}
    args = {**args, "envs": envs or None, "_env_meta": meta, "_analysis": an}
    args.setdefault("platform", an["platform"])
    if an.get("port") and not args.get("port"):
        args["port"] = an["port"]
    if args.get("create") and not args.get("plan_id"):
        args["plan_id"] = (an.get("plan") or {}).get("id")
    ctx.state["last_env_meta"] = meta
    return args


# ============================================================ READ (auto-run)
@tool("account_overview", "اطلاعات حساب لیارا، موجودی و تعداد اپ‌ها را برمی‌گرداند.")
async def account_overview(ctx: Ctx):
    me = await ctx.client.me()
    apps = await ctx.client.list_apps()
    u = me.get("user", {})
    return {"email": u.get("email"), "balance": u.get("balance"), "apps": len(apps),
            "mock": getattr(ctx.client, "is_mock", False)}


@tool("list_apps", "لیست اپ‌های حساب لیارا با وضعیت و پلن.")
async def list_apps(ctx: Ctx):
    apps = await ctx.client.list_apps()
    return [{"name": a.get("project_id") or a.get("name"), "platform": a.get("type"), "plan": a.get("planID"),
             "status": a.get("status"), "scale": a.get("scale"), "url": app_url(a.get("project_id") or a.get("name"))}
            for a in apps]


@tool("app_status", "وضعیت لحظه‌ای یک اپ: روشن/خاموش، پلن، پورت، نام متغیرهای محیطی و آخرین ریلیز.",
      {"app": APP}, required=("app",))
async def app_status(ctx: Ctx, app: str):
    d = await ctx.client.get_app(app)
    rel = await ctx.client.list_releases(app, 3)
    pr = await diagnosis.probe(ctx.client, app)
    return {"app": app, "status": d.get("status"), "scale": d.get("scale"), "plan": d.get("planID"),
            "platform": d.get("type"), "port": d.get("port"),
            "env_keys": [e.get("key") for e in d.get("envs", [])],  # values hidden on purpose
            "last_releases": [{"id": r.get("_id"), "state": r.get("state"), "tag": r.get("tag"),
                               "at": r.get("createdAt")} for r in rel],
            "http_check": pr, "url": app_url(app)}


@tool("get_logs", "لاگ‌های اخیر اپ را می‌گیرد. می‌توان با query فیلتر کرد (مثلاً error).",
      {"app": APP, "minutes": {"type": "integer", "default": 30},
       "query": {"type": "string", "description": "فیلتر متنی اختیاری"}}, required=("app",))
async def get_logs(ctx: Ctx, app: str, minutes: int = 30, query: str | None = None):
    lines = await ctx.client.logs(app, minutes=minutes)
    if query:
        lines = [l for l in lines if query.lower() in l["line"].lower()]
    return {"count": len(lines), "lines": [l["line"] for l in lines[-60:]]}


@tool("get_metrics", "مصرف CPU و RAM اپ (در صورت در دسترس بودن از API).", {"app": APP}, required=("app",))
async def get_metrics(ctx: Ctx, app: str):
    try:
        return await ctx.client.metrics(app)
    except Limitation as e:
        return {"limitation": str(e)}


@tool("list_releases", "تاریخچه‌ی نسخه‌های منتشرشده‌ی اپ.", {"app": APP}, required=("app",))
async def list_releases(ctx: Ctx, app: str):
    rel = await ctx.client.list_releases(app, 10)
    return [{"id": r.get("_id"), "state": r.get("state"), "tag": r.get("tag"), "at": r.get("createdAt")} for r in rel]


async def _paid_plans(ctx: Ctx) -> dict:
    plans = await ctx.client.creatable_plans()
    paid = {k: v for k, v in plans.items() if k != "free"}
    return paid or plans


@tool("list_plans", "پلن‌های اپ (PaaS) که الان در لیارا قابل‌ساخت هستند (نسل g2، منطقه‌ی ایران) با RAM/CPU/دیسک/قیمت ماهانه.")
async def list_plans(ctx: Ctx):
    plans = await _paid_plans(ctx)
    return analyzer.recommend_plan(plans, 0).get("all", plans)


@tool("analyze_project",
      "پروژه‌ی کاربر (مسیر پوشه یا لینک گیت‌هاب) را تحلیل می‌کند: نوع پلتفرم، پورت، متغیرهای محیطی لازم و پلن پیشنهادی.",
      {"source": {"type": "string", "description": "مسیر پوشه یا URL مخزن git"}}, required=("source",))
async def analyze_project(ctx: Ctx, source: str):
    await ctx.step("خواندن پروژه و تشخیص نوع آن")
    plans = await _paid_plans(ctx)
    res = await asyncio.to_thread(analyzer.analyze, source, plans)
    res["source"] = source
    ctx.state["analysis"] = res
    await ctx.step(f"پروژه از نوع {res['platform_fa']} است", "done")
    return res


@tool("diagnose",
      "عیب‌یابی کامل SRE: لاگ، وضعیت، ریلیزها، متریک و تست HTTP را جمع می‌کند و علت‌های محتمل را با شواهد و راه‌حل اولویت‌بندی می‌کند. برای «سایت کند است»، «خطای ۵۰۰»، «سایت بالا نمی‌آید» استفاده کن.",
      {"app": APP, "symptom": {"type": "string", "description": "توصیف مشکل از زبان کاربر"}}, required=("app",))
async def diagnose(ctx: Ctx, app: str, symptom: str = "", _build_tail: list | None = None,
                   _release_logs: list | None = None):
    await ctx.step("جمع‌آوری شواهد: وضعیت سرویس، لاگ‌ها، رویدادها، ریلیزها، متریک‌ها، متغیرهای محیطی و تست مستقیم سایت")
    ev = await diagnosis.collect_evidence(ctx.client, app)
    src = app_source(ctx, app)
    if src:
        ev["source"] = src
        try:
            an = ctx.state.get("analysis") if (ctx.state.get("analysis") or {}).get("source") == src else \
                await asyncio.to_thread(analyzer.analyze, src, None)
            ev["runtime"] = an.get("runtime")
        except Exception:
            pass
    try:
        ev["env_check"] = await _env_check(ctx, app)
    except Exception:
        pass
    if _release_logs:
        seen = {l["line"] for l in _release_logs}
        ev["logs"] = [l for l in ev["logs"] if l["line"] not in seen] + _release_logs
        ev["release_logs"] = [l["line"] for l in _release_logs]
    if _build_tail:
        ev["logs"] = ev["logs"] + [{"ts": 0, "line": f"[build] {l}", "releaseId": None} for l in _build_tail]
        ev["build_logs"] = list(_build_tail)
    await ctx.step(f"{len(ev['logs'])} خط لاگ و {len(ev['releases'])} ریلیز بررسی شد", "done")
    await ctx.step("تحلیل شواهد و رتبه‌بندی علت‌های محتمل")
    rep = diagnosis.analyze(ev)
    ctx.state.setdefault("diagnoses", {})[app] = rep
    n = len(rep["hypotheses"])
    await ctx.step(f"{n} علت محتمل پیدا شد" if n else "مشکلی پیدا نشد", "done")
    await ctx.emit({"type": "diagnosis", "app": app, "report": rep})
    return rep


from . import config as _config  # noqa: E402
async def _wait_release_logs(ctx: Ctx, app: str, rid: str, wait_s: int = 45) -> list[dict]:
    """After an UNHEALTHY release Liara keeps restarting the container; its stdout/stderr (the real
    crash reason) shows up in the app logs a few seconds later. Poll the logs of *this* release."""
    mock = getattr(ctx.client, "is_mock", False)
    t0, lines = time.time(), []
    while True:
        try:
            lines = await ctx.client.logs(app, minutes=20, limit=400, release_id=rid)
        except Exception:
            lines = []
        crash = any(re.search(r"(Traceback|Error|Exception|exited|not found|No such file)", l["line"]) for l in lines)
        if crash or mock or time.time() - t0 > wait_s:
            return lines
        await asyncio.sleep(5)


APP_SOURCES_FILE = os.path.join(_config.DATA_DIR, "app_sources.json")


def _load_sources() -> dict:
    try:
        import json
        with open(APP_SOURCES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def remember_source(ctx: Ctx, app: str, source: str):
    """Remember which project folder/repo an app was deployed from (survives restarts)."""
    import json
    ctx.state.setdefault("app_sources", {})[app] = source
    data = _load_sources()
    data[app] = source
    try:
        os.makedirs(os.path.dirname(APP_SOURCES_FILE), exist_ok=True)
        with open(APP_SOURCES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def app_source(ctx: Ctx, app: str) -> str | None:
    return (ctx.state.get("app_sources") or {}).get(app) or _load_sources().get(app)


SUSPICIOUS = re.compile(r"^(<.*>|changeme|change_me|your[_-].*|xxx+|todo|example|test|placeholder|\*+|none|null)$", re.I)


async def _env_check(ctx: Ctx, app: str, source: str | None = None) -> dict:
    """Compare env vars the code uses with what is set on Liara. Values are never returned."""
    source = source or app_source(ctx, app)
    on = {e["key"]: (e.get("value") or "") for e in await ctx.client.get_envs(app)}
    needed: dict = {}
    if source:
        an = ctx.state.get("analysis") if (ctx.state.get("analysis") or {}).get("source") == source else \
            await asyncio.to_thread(analyzer.analyze, source, None)
        needed = an.get("env_vars", {})
    missing = [k for k in needed if k not in on]
    empty = [k for k, v in on.items() if not str(v).strip()]
    suspicious = [k for k, v in on.items() if str(v).strip() and
                  (SUSPICIOUS.match(str(v).strip()) or (needed.get(k, {}).get("example") and v == needed[k]["example"]))]
    rows = [{"key": k, "status": "missing", "used_at": needed[k].get("used_at")} for k in missing]
    rows += [{"key": k, "status": "empty", "used_at": needed.get(k, {}).get("used_at")} for k in empty]
    rows += [{"key": k, "status": "suspicious", "used_at": needed.get(k, {}).get("used_at")} for k in suspicious]
    rows += [{"key": k, "status": "ok", "used_at": needed.get(k, {}).get("used_at")} for k in on
             if k not in empty and k not in suspicious]
    return {"app": app, "source_known": bool(source), "missing": missing, "empty": empty,
            "suspicious": suspicious, "rows": rows}


@tool("check_envs",
      "بررسی متغیرهای محیطی اپ: کدام‌ها در کد استفاده شده ولی روی لیارا نیستند، کدام خالی‌اند و کدام مقدار مشکوک "
      "(نمونه/پیش‌فرض) دارند. مقدارها را برنمی‌گرداند. بعد از دیپلوی یا وقتی برنامه خطا می‌دهد استفاده کن.",
      {"app": APP, "source": {"type": "string", "description": "مسیر پروژه (اختیاری؛ اگر قبلاً دیپلوی شده لازم نیست)"}},
      required=("app",))
async def check_envs(ctx: Ctx, app: str, source: str | None = None):
    res = await _env_check(ctx, app, source)
    if not res["source_known"]:
        res["note"] = "مسیر پروژه‌ی این اپ را نمی‌دانم؛ فقط متغیرهای خالی/مشکوک روی لیارا بررسی شد."
    return res


@tool("verify_fix", "بعد از اقدام اصلاحی، دوباره شواهد را جمع می‌کند و با قبل مقایسه می‌کند تا مطمئن شود مشکل حل شده.",
      {"app": APP}, required=("app",))
async def verify_fix(ctx: Ctx, app: str):
    before = ctx.state.get("diagnoses", {}).get(app) or {"hypotheses": [], "summary": {}}
    await ctx.step("راستی‌آزمایی: تست دوباره‌ی سایت و بررسی لاگ‌های جدید")
    since = ctx.state.get("last_fix_at", {}).get(app)
    res = await diagnosis.verify(ctx.client, app, before, wait_s=0 if getattr(ctx.client, "is_mock", False) else 20,
                                 since=since)
    ctx.state.setdefault("diagnoses", {})[app] = res["report"]
    await ctx.step("مشکل برطرف شد ✅" if res["fixed"] else "مشکل هنوز کامل حل نشده ⚠️",
                   "done" if res["fixed"] else "warn")
    await ctx.emit({"type": "verification", "app": app, "result": {k: v for k, v in res.items() if k != "report"}})
    return {k: v for k, v in res.items() if k != "report"} | {"remaining_hypotheses": res["report"]["hypotheses"]}


@tool("health_check_all", "پایش یکجای سلامت همه‌ی اپ‌ها (خودکارسازی بررسی روزانه).")
async def health_check_all(ctx: Ctx):
    apps = await ctx.client.list_apps()
    out = []
    for a in apps:
        name = a.get("project_id") or a.get("name")
        await ctx.step(f"بررسی {name}")
        ev = await diagnosis.collect_evidence(ctx.client, name, minutes=15)
        rep = diagnosis.analyze(ev)
        ctx.state.setdefault("diagnoses", {})[name] = rep
        top = rep["hypotheses"][0]["title"] if rep["hypotheses"] else None
        out.append({"app": name, "status": rep["summary"]["status"], "latency_ms": rep["summary"]["probe"].get("median_ms"),
                    "top_issue": top})
        await ctx.step(f"{name}: {rep['summary']['status']}", "done" if not top else "warn")
    await ctx.emit({"type": "health", "apps": out})
    return out


@tool("list_domains", "دامنه‌های متصل به اپ و وضعیت اتصالشان.", {"app": APP}, required=("app",))
async def list_domains(ctx: Ctx, app: str):
    return await ctx.client.list_domains(app)


@tool("domain_dns_guide",
      "راهنمای قدم‌به‌قدم تنظیم DNS در پنل ثبت دامنه (مثل ایرنیک/nic.ir) برای اتصال دامنه به اپ لیارا.",
      {"app": APP, "domain": {"type": "string"}}, required=("app", "domain"))
async def domain_dns_guide(ctx: Ctx, app: str, domain: str):
    is_ir = domain.endswith(".ir")
    sub = domain.count(".") >= (2 if not is_ir else 2) and not domain.startswith("www.") and domain.count(".") > 1
    return {
        "who_does_what": "این مراحل در پنل ثبت‌کننده‌ی دامنه انجام می‌شود و از طریق API لیارا قابل انجام نیست؛ "
                         "دستیار مقادیر را آماده می‌کند و شما فقط وارد می‌کنید.",
        "option_A_recommended": {
            "title": "سپردن مدیریت DNS به لیارا (ساده‌ترین)",
            "steps": [
                "در لیارا: بخش DNS → افزودن دامنه → ‎%s‎ (یا به دستیار بگویید)." % domain,
                ("در nic.ir وارد شوید → «دامنه‌های من» → ‎%s‎ → «تغییر سرورهای نام»." % domain) if is_ir
                else "در پنل ثبت‌کننده → تنظیمات Nameserver.",
                "سرورهای نامی که لیارا در صفحه‌ی همان دامنه نمایش می‌دهد را جایگزین کنید (معمولاً ns1/ns2 لیارا).",
                "اعمال تغییر در ایرنیک ممکن است چند ساعت طول بکشد.",
            ],
        },
        "option_B": {
            "title": "نگه داشتن DNS فعلی",
            "records": [
                {"type": "CNAME", "name": "www" if not sub else domain.split(".")[0], "value": f"{app}.liara.run"},
                {"type": "ALIAS/ANAME یا A", "name": "@", "value": "مقداری که لیارا در صفحه‌ی دامنه‌ی اپ نشان می‌دهد"},
            ],
        },
        "then": "بعد از اعمال DNS، دستیار با ابزار check وضعیت را بررسی و SSL رایگان را فعال می‌کند (از پنل لیارا).",
    }


@tool("audit_log", "تاریخچه‌ی عملیات انجام‌شده توسط دستیار (برای شفافیت و امنیت).")
async def audit_log(ctx: Ctx):
    from .policy import read_audit
    return read_audit(30)


@tool("suggest_plan", "بر اساس مصرف فعلی اپ، پلن مناسب‌تر (بزرگ‌تر یا کوچک‌تر) را پیشنهاد می‌دهد.",
      {"app": APP, "direction": {"type": "string", "enum": ["up", "down"], "description": "up=بزرگ‌تر، down=کوچک‌تر"}},
      required=("app",))
async def suggest_plan(ctx: Ctx, app: str, direction: str = "up"):
    d = await ctx.client.get_app(app)
    plans = await _paid_plans(ctx)
    cur = (await ctx.client.list_plans()).get(d.get("planID"), {})
    ram = analyzer._plan_ram(cur) or 0.5
    rows = analyzer.recommend_plan(plans, 0).get("all", [])
    if direction == "down":
        cand = [r for r in rows if r["ram_gb"] < ram]
        rec = cand[-1] if cand else None
    else:
        rec = next((r for r in rows if r["ram_gb"] > ram), None)
    return {"current_plan": d.get("planID"), "current_ram_gb": ram, "suggested": rec,
            "how": "با ابزار change_plan (با تأیید کاربر) اعمال می‌شود."}


# ===================================================== WRITE (policy-guarded)
@tool("create_app", "ساخت اپ جدید در لیارا با پلتفرم و پلن مشخص (هزینه دارد).",
      {"name": {"type": "string"}, "platform": {"type": "string"}, "plan_id": {"type": "string"}},
      risk=Risk.HIGH, required=("name", "platform", "plan_id"),
      approval=lambda a: (f"ساخت اپ «{a['name']}» با پلتفرم {a['platform']} و پلن {a['plan_id']}",
                          "از اعتبار حساب لیارا به‌صورت ساعتی هزینه کم می‌شود.",
                          "با حذف اپ، هزینه متوقف می‌شود."))
async def create_app(ctx: Ctx, name: str, platform: str, plan_id: str):
    plans = await _paid_plans(ctx)
    if plan_id not in plans:
        return {"error": f"پلن {plan_id} برای ساخت اپ جدید مجاز نیست (فقط پلن‌های نسل g2). پلن‌های مجاز: "
                         f"{', '.join(list(plans)[:12])}"}
    return await ctx.client.create_app(name, platform, plan_id)


def _deploy_approval(a):
    an = a.get("_analysis") or {}
    n_env = len(a.get("envs") or {})
    rt = (an.get("runtime") or {})
    start = a.get("start_command") or ((rt.get("args") or [None])[0])
    return (f"انتشار پروژه روی اپ «{a['app']}»" + (" (اپ جدید ساخته می‌شود)" if a.get("create") else ""),
            f"پلتفرم {a.get('platform') or an.get('platform')}، "
            + (f"پورت {a.get('port') or an.get('port')}" if (a.get('port') or an.get('port')) else "بدون پورت (برنامه‌ی غیر وب)")
            + (f"، پلن {a.get('plan_id')}" if a.get("create") else "")
            + (f"، {n_env} متغیر محیطی اختیاری (پایین همین کارت)" if n_env else "، بدون متغیر محیطی")
            + ". دیپلوی بدون قطعی انجام می‌شود اما نسخه‌ی فعلی جایگزین می‌شود."
            + (f"\nدستور اجرا: {start}" if start else "")
            + (f"\nافزودن به requirements (فقط در بسته‌ی آپلود): {', '.join(rt['inject_requirements'])}" if rt.get("inject_requirements") else "")
            + "".join(f"\n• {n}" for n in rt.get("notes", [])),
            "در صورت مشکل، به نسخه‌ی قبلی rollback می‌کنم.")


@tool("deploy",
      "کل فرایند انتشار: (ساخت اپ در صورت نیاز) → تنظیم متغیرهای محیطی → بسته‌بندی سورس → آپلود → build → اجرا → تست سلامت. قبلش analyze_project را اجرا کن.",
      {"source": {"type": "string", "description": "مسیر پوشه یا URL گیت"}, "app": APP,
       "create": {"type": "boolean", "description": "اگر اپ وجود ندارد، ساخته شود"},
       "plan_id": {"type": "string"}, "platform": {"type": "string"}, "port": {"type": "integer"},
       "envs": {"type": "object", "description": "فقط مقدارهایی که کاربر صریحاً گفته {KEY: VALUE}"},
       "start_command": {"type": "string", "description": "دستور اجرای سفارشی؛ فقط اگر تحلیل/عیب‌یابی نشان داد پیش‌فرض اشتباه است"}},
      risk=Risk.HIGH, required=("source", "app"), approval=_deploy_approval, prepare=prepare_deploy)
async def deploy(ctx: Ctx, source: str, app: str, create: bool = False, plan_id: str | None = None,
                 platform: str | None = None, port: int | None = None, envs: dict | None = None,
                 start_command: str | None = None):
    an = ctx.state.get("analysis")
    if not an or an.get("source") != source:
        an = await asyncio.to_thread(analyzer.analyze, source, await _paid_plans(ctx))
        an["source"] = source
    rt = an.get("runtime") or {}
    platform = platform or an["platform"]
    port = port or an.get("port")
    args = [start_command] if start_command else rt.get("args")
    if create:
        plan_id = plan_id or an["plan"]["id"]
        await ctx.step(f"ساخت اپ {app} روی پلن {plan_id} (انتخاب خودکار بسته‌ی امکانات و شبکه‌ی خصوصی)")
        try:
            cr = await ctx.client.create_app(app, platform, plan_id)
            await ctx.step(f"اپ ساخته شد — پلن {cr.get('planID')}، بسته‌ی امکانات {cr.get('bundlePlanID')}", "done")
        except LiaraError as e:
            if e.status != 409:
                raise
            mine = [a.get("project_id") or a.get("name") for a in await ctx.client.list_apps()]
            if app not in mine:
                await ctx.step(f"نام «{app}» قبلاً در لیارا توسط کاربر دیگری گرفته شده", "error")
                return {"error": f"نام «{app}» در لیارا آزاد نیست؛ نام دیگری انتخاب کنید (مثلاً {app}-{int(time.time()) % 10000})."}
            await ctx.step("اپ از قبل وجود دارد؛ همان را به‌روزرسانی می‌کنم", "done")
    remember_source(ctx, app, source)
    envs = {k: str(v) for k, v in (envs or {}).items() if v not in (None, "") and not str(v).startswith("<")}
    if envs:
        await ctx.step(f"تنظیم {len(envs)} متغیر محیطی (مقادیر در لاگ ذخیره نمی‌شوند)")
        await ctx.client.set_envs(app, envs)
        await ctx.step("متغیرها تنظیم شد", "done")
    inject = rt.get("inject_requirements") or []
    await ctx.step("بسته‌بندی سورس (با رعایت ‎.gitignore‎ و بدون ‎.env‎)"
                   + (f" + افزودن {', '.join(inject)} به requirements" if inject else ""))
    archive = await asyncio.to_thread(analyzer.package, an["path"], inject)
    size = os.path.getsize(archive) / 1e6
    await ctx.step(f"بسته آماده شد ({size:.1f}MB)", "done")
    await ctx.step("آپلود سورس به لیارا")
    sid = await ctx.client.upload_source(app, archive)
    await ctx.step("آپلود کامل شد", "done")
    run_desc = f"اجرا با ‎{args[0]}‎" if args else "دستور اجرای پیش‌فرض پلتفرم"
    await ctx.step(f"شروع build و انتشار — {run_desc}" + (f"، پورت {port}" if port else "، بدون پورت (غیر وب)"))
    rel = await ctx.client.create_release(app, sid, platform, port, platform_config=rt.get("platform_config") or {},
                                          args=args)
    rid = rel.get("releaseID") or rel.get("_id") or (rel.get("release") or {}).get("_id")
    state, last, t0, tail, build_all, since = "", "", time.time(), [], [], None
    while time.time() - t0 < 900:
        r = await ctx.client.get_release(rid)
        state = str(r.get("state", "")).upper()
        try:
            bl = await ctx.client.build_logs(rid, since) if since else await ctx.client.build_logs(rid)
            out_lines = bl.get("buildOutput", []) or []
            if out_lines:
                since = out_lines[-1].get("createdAt") or since
                new = [o.get("line", "") for o in out_lines]
                build_all = (build_all + [l for l in new if not build_all or l not in build_all[-len(new):]])[-400:]
                tail = build_all[-8:]
        except Exception:
            pass
        if state != last:
            fa = {"BUILDING": "در حال build", "PUSHING": "ذخیره‌ی image", "DEPLOYING": "در حال اجرا روی سرور",
                  "READY": "آماده", "UNHEALTHY": "برنامه بالا آمد ولی به بررسی سلامت جواب نداد",
                  "FAILED": "ناموفق"}.get(state, state)
            await ctx.step(f"وضعیت ریلیز: {fa}", "done" if state == "READY" else "error" if state in ("FAILED", "UNHEALTHY") else "running",
                           detail="\n".join(tail[-3:]) if tail else None)
            last = state
        if state in ("READY", "FAILED", "UNHEALTHY", "CANCELED"):
            break
        await asyncio.sleep(0.3 if getattr(ctx.client, "is_mock", False) else 4)
    ok = state == "READY"
    is_web = rt.get("is_web", True)
    pr = await diagnosis.probe(ctx.client, app) if ok and is_web else {}
    result = {"release_id": rid, "state": state, "url": app_url(app) if is_web else None, "http_check": pr,
              "build_tail": tail, "start": args[0] if args else "default", "port": port}
    missing_env = [k for k, m in (ctx.state.get("last_env_meta") or {}).items() if k not in envs and not m.get("on_liara")]
    if missing_env:
        result["unset_env_vars"] = missing_env
    if not ok or (is_web and pr and not pr.get("ok")):
        await ctx.step("انتشار سالم نبود؛ لاگ‌های همین نسخه را برای پیدا کردن علت می‌خوانم", "error")
        rlogs = await _wait_release_logs(ctx, app, rid)
        if rlogs:
            await ctx.step(f"{len(rlogs)} خط لاگ اجرای همین نسخه دریافت شد", "done",
                           detail="\n".join(l["line"] for l in rlogs[-4:]))
        else:
            await ctx.step("لیارا هنوز لاگ اجرایی برای این نسخه نفرستاده؛ تحلیل با لاگ build و وضعیت انجام می‌شود", "warn")
        result["auto_diagnosis"] = await diagnose(ctx, app, "deploy failed", _build_tail=build_all[-60:],
                                                  _release_logs=rlogs)
        result["release_log_tail"] = [l["line"] for l in rlogs[-25:]]
    else:
        await ctx.step(f"سایت بالا آمد: {app_url(app)}" if is_web else "برنامه اجرا شد (بدون وب‌سرور)", "done")
        if missing_env:
            await ctx.step(f"این متغیرها خالی ماندند: {', '.join(missing_env)} — هر وقت مقدارشان را بدهید تنظیم می‌کنم", "warn")
    return result


@tool("change_plan", "تغییر پلن (منابع CPU/RAM/دیسک) اپ از طریق API لیارا. اپ ری‌استارت می‌شود و هزینه تغییر می‌کند.",
      {"app": APP, "plan_id": {"type": "string", "description": "شناسه‌ی پلن g2 از list_plans"}},
      risk=Risk.HIGH, required=("app", "plan_id"),
      approval=lambda a: (f"تغییر پلن «{a['app']}» به {a['plan_id']}",
                          "اپ چند ثانیه ری‌استارت می‌شود و هزینه‌ی ساعتی بر اساس پلن جدید محاسبه می‌شود.",
                          "با تغییر دوباره به پلن قبلی."))
async def change_plan(ctx: Ctx, app: str, plan_id: str):
    plans = await ctx.client.list_plans()
    if plan_id not in plans:
        return {"error": f"پلن {plan_id} وجود ندارد. پلن‌های مجاز: {', '.join(list((await _paid_plans(ctx)).keys())[:12])}"}
    res = await ctx.client.change_plan(app, plan_id)
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True, "new_plan": plan_id, "api": res}


@tool("set_env", "افزودن/تغییر متغیرهای محیطی اپ. توجه: اپ ری‌استارت می‌شود.",
      {"app": APP, "variables": {"type": "object"}, "remove": {"type": "array", "items": {"type": "string"}}},
      risk=Risk.HIGH, required=("app", "variables"),
      approval=lambda a: (f"تنظیم متغیرهای {', '.join(a.get('variables', {}).keys())} روی «{a['app']}»"
                          + (f" و حذف {', '.join(a.get('remove') or [])}" if a.get("remove") else ""),
                          "اپ برای اعمال تغییرات چند ثانیه ری‌استارت می‌شود.",
                          "مقدار قبلی در لیارا قابل برگرداندن است."))
async def set_env(ctx: Ctx, app: str, variables: dict, remove: list | None = None):
    res = await ctx.client.set_envs(app, {k: str(v) for k, v in variables.items()}, remove)
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True, "message": "متغیرها اعمال شد و اپ در حال ری‌استارت است", "api": res}


@tool("restart_app", "ری‌استارت اپ (چند ثانیه قطعی).", {"app": APP}, risk=Risk.HIGH, required=("app",),
      approval=lambda a: (f"ری‌استارت اپ «{a['app']}»", "حدود ۱۰ تا ۳۰ ثانیه سایت در دسترس نیست.",
                          "اثر ماندگاری ندارد."))
async def restart_app(ctx: Ctx, app: str):
    await ctx.client.restart_app(app)
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True}


@tool("start_app", "روشن کردن اپ خاموش.", {"app": APP}, risk=Risk.HIGH, required=("app",),
      approval=lambda a: (f"روشن کردن «{a['app']}»", "هزینه‌ی ساعتی پلن دوباره محاسبه می‌شود.", "با خاموش کردن."))
async def start_app(ctx: Ctx, app: str):
    await ctx.client.scale_app(app, 1)
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True}


@tool("stop_app", "خاموش کردن اپ.", {"app": APP}, risk=Risk.HIGH, required=("app",),
      approval=lambda a: (f"خاموش کردن «{a['app']}»", "سایت تا روشن شدن دوباره از دسترس خارج می‌شود.",
                          "با روشن کردن دوباره."))
async def stop_app(ctx: Ctx, app: str):
    return await ctx.client.scale_app(app, 0)


@tool("update_port", "تغییر پورتی که لیارا ترافیک را به آن می‌فرستد.", {"app": APP, "port": {"type": "integer"}},
      risk=Risk.HIGH, required=("app", "port"),
      approval=lambda a: (f"تغییر پورت «{a['app']}» به {a['port']}", "اپ ری‌استارت می‌شود.", "برگرداندن پورت قبلی."))
async def update_port(ctx: Ctx, app: str, port: int):
    await ctx.client.update_port(app, port)
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True}


@tool("rollback", "برگرداندن اپ به یک نسخه‌ی سالم قبلی.", {"app": APP, "release_id": {"type": "string"}},
      risk=Risk.HIGH, required=("app", "release_id"),
      approval=lambda a: (f"برگرداندن «{a['app']}» به نسخه‌ی {a['release_id'][:8]}",
                          "نسخه‌ی فعلی (خراب) کنار گذاشته می‌شود.", "می‌توان دوباره نسخه‌ی جدید را منتشر کرد."))
async def rollback(ctx: Ctx, app: str, release_id: str):
    try:
        res = await ctx.client.rollback(app, release_id)
    except Limitation as e:
        return {"limitation": str(e)}
    ctx.state.setdefault("last_fix_at", {})[app] = time.time()
    return {"ok": True, "api": res}


@tool("add_domain", "اتصال دامنه به اپ در لیارا (بدون قطعی).", {"app": APP, "domain": {"type": "string"}},
      risk=Risk.LOW, required=("app", "domain"),
      approval=lambda a: (f"اتصال {a['domain']} به «{a['app']}»", "بدون قطعی.", "حذف دامنه از پنل."))
async def add_domain(ctx: Ctx, app: str, domain: str):
    return await ctx.client.add_domain(app, domain)


@tool("delete_app", "حذف کامل اپ (غیرقابل بازگشت).", {"app": APP}, risk=Risk.CRITICAL, required=("app",),
      approval=lambda a: (f"حذف کامل اپ «{a['app']}»", "همه‌ی فایل‌ها، دیسک‌ها و تنظیمات پاک می‌شوند.",
                          "⛔ غیرقابل بازگشت است."))
async def delete_app(ctx: Ctx, app: str):
    return await ctx.client.delete_app(app)


# IaaS (virtual machine) tools register themselves on import
from . import vm_tools  # noqa: E402,F401
