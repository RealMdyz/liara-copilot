"""Tool registry shared by the chat agent and the MCP server.

Each tool = schema + risk level + plain-Persian approval text + async implementation.
"""
from __future__ import annotations

import asyncio
import os
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
    """Make sure the user is asked for every environment variable the project needs.

    Sources, in priority order:
      1. values the user already gave (in chat / by the LLM)        -> optional input, pre-filled
      2. the project's own .env file (never uploaded)                -> optional input, pre-filled
      3. already configured on the Liara app                         -> kept, not asked again
      4. otherwise                                                   -> REQUIRED input in the approval card
    Example values from .env.example are only shown as a hint, never used silently.
    """
    source = args.get("source")
    an = ctx.state.get("analysis")
    if not an or an.get("source") != source:
        an = await asyncio.to_thread(analyzer.analyze, source, await _paid_plans(ctx))
        an["source"] = source
        ctx.state["analysis"] = an
    from pathlib import Path
    dotenv = analyzer.dotenv_values(Path(an["path"]))
    given = {k: str(v) for k, v in (args.get("envs") or {}).items()
             if v not in (None, "") and not str(v).startswith("<")}
    on_liara: set = set()
    if not args.get("create"):
        try:
            on_liara = {e["key"] for e in await ctx.client.get_envs(args["app"])}
        except Exception:
            pass
    keys = list(dict.fromkeys(list(an.get("env_vars", {})) + list(args.get("envs") or {}) + list(dotenv)))
    envs, meta = {}, {}
    for k in keys:
        info = an.get("env_vars", {}).get(k, {})
        ex = info.get("example", "")
        where = info.get("used_at", "")
        if k in given:
            envs[k], meta[k] = given[k], {"required": False, "hint": "مقدار داده‌شده در گفت‌وگو (برای تغییر، مقدار جدید بنویسید)"}
        elif k in dotenv and dotenv[k]:
            envs[k], meta[k] = dotenv[k], {"required": False, "hint": "از فایل ‎.env‎ پروژه‌ی شما خوانده شد (خالی = همان مقدار)"}
        elif k in on_liara:
            continue
        else:
            envs[k] = f"<{k}>"
            hint = f"در کد: {where}" if where else ""
            if ex and not any(w in k.upper() for w in SECRETY):
                hint += (" · " if hint else "") + f"نمونه: {ex}"
            meta[k] = {"required": True, "hint": hint or "مقدار را وارد کنید"}
    args = {**args, "envs": envs or None, "_env_meta": meta, "_analysis": an}
    args.setdefault("platform", an["platform"])
    args.setdefault("port", an["port"])
    if args.get("create") and not args.get("plan_id"):
        args["plan_id"] = (an.get("plan") or {}).get("id")
    ctx.state["env_kept_on_liara"] = sorted(on_liara & set(keys))
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
async def diagnose(ctx: Ctx, app: str, symptom: str = ""):
    await ctx.step("جمع‌آوری شواهد: وضعیت سرویس، لاگ‌ها، ریلیزها، متریک‌ها و تست مستقیم سایت")
    ev = await diagnosis.collect_evidence(ctx.client, app)
    await ctx.step(f"{len(ev['logs'])} خط لاگ و {len(ev['releases'])} ریلیز بررسی شد", "done")
    await ctx.step("تحلیل شواهد و رتبه‌بندی علت‌های محتمل")
    rep = diagnosis.analyze(ev)
    ctx.state.setdefault("diagnoses", {})[app] = rep
    n = len(rep["hypotheses"])
    await ctx.step(f"{n} علت محتمل پیدا شد" if n else "مشکلی پیدا نشد", "done")
    await ctx.emit({"type": "diagnosis", "app": app, "report": rep})
    return rep


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
    return (f"انتشار پروژه روی اپ «{a['app']}»" + (" (اپ جدید ساخته می‌شود)" if a.get("create") else ""),
            f"پلتفرم {a.get('platform') or an.get('platform')}، پورت {a.get('port') or an.get('port')}"
            + (f"، پلن {a.get('plan_id')}" if a.get("create") else "")
            + (f"، {n_env} متغیر محیطی (پایین همین کارت)" if n_env else "، بدون متغیر محیطی جدید")
            + ". دیپلوی بدون قطعی انجام می‌شود اما نسخه‌ی فعلی جایگزین می‌شود.",
            "در صورت مشکل، به نسخه‌ی قبلی rollback می‌کنم.")


@tool("deploy",
      "کل فرایند انتشار: (ساخت اپ در صورت نیاز) → تنظیم متغیرهای محیطی → بسته‌بندی سورس → آپلود → build → اجرا → تست سلامت. قبلش analyze_project را اجرا کن.",
      {"source": {"type": "string", "description": "مسیر پوشه یا URL گیت"}, "app": APP,
       "create": {"type": "boolean", "description": "اگر اپ وجود ندارد، ساخته شود"},
       "plan_id": {"type": "string"}, "platform": {"type": "string"}, "port": {"type": "integer"},
       "envs": {"type": "object", "description": "متغیرهای محیطی {KEY: VALUE}"}},
      risk=Risk.HIGH, required=("source", "app"), approval=_deploy_approval, prepare=prepare_deploy)
async def deploy(ctx: Ctx, source: str, app: str, create: bool = False, plan_id: str | None = None,
                 platform: str | None = None, port: int | None = None, envs: dict | None = None):
    an = ctx.state.get("analysis")
    if not an or an.get("source") != source:
        an = await asyncio.to_thread(analyzer.analyze, source, await _paid_plans(ctx))
        an["source"] = source
    platform = platform or an["platform"]
    port = port or an["port"]
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
    if envs:
        await ctx.step(f"تنظیم {len(envs)} متغیر محیطی (مقادیر در لاگ ذخیره نمی‌شوند)")
        await ctx.client.set_envs(app, {k: str(v) for k, v in envs.items()})
        await ctx.step("متغیرها تنظیم شد", "done")
    await ctx.step("بسته‌بندی سورس (بدون node_modules، .git و .env)")
    archive = await asyncio.to_thread(analyzer.package, an["path"])
    size = os.path.getsize(archive) / 1e6
    await ctx.step(f"بسته آماده شد ({size:.1f}MB)", "done")
    await ctx.step("آپلود سورس به لیارا")
    sid = await ctx.client.upload_source(app, archive)
    await ctx.step("آپلود کامل شد", "done")
    await ctx.step("شروع build و انتشار")
    rel = await ctx.client.create_release(app, sid, platform, port)
    rid = rel.get("releaseID") or rel.get("_id") or (rel.get("release") or {}).get("_id")
    state, last, t0, tail = "", "", time.time(), []
    while time.time() - t0 < 900:
        r = await ctx.client.get_release(rid)
        state = str(r.get("state", "")).upper()
        try:
            bl = await ctx.client.build_logs(rid)
            tail = [o.get("line", "") for o in bl.get("buildOutput", [])][-5:] or tail
        except Exception:
            pass
        if state != last:
            fa = {"BUILDING": "در حال build", "PUSHING": "ذخیره‌ی image", "DEPLOYING": "در حال اجرا روی سرور",
                  "READY": "آماده"}.get(state, state)
            await ctx.step(f"وضعیت ریلیز: {fa}", "done" if state == "READY" else "running",
                           detail="\n".join(tail[-3:]) if tail else None)
            last = state
        if state in ("READY", "FAILED", "UNHEALTHY", "CANCELED"):
            break
        await asyncio.sleep(0.3 if getattr(ctx.client, "is_mock", False) else 4)
    ok = state == "READY"
    pr = await diagnosis.probe(ctx.client, app) if ok else {}
    result = {"release_id": rid, "state": state, "url": app_url(app), "http_check": pr, "build_tail": tail}
    if not ok:
        await ctx.step("انتشار ناموفق بود؛ عیب‌یابی خودکار را شروع می‌کنم", "error")
        result["auto_diagnosis"] = await diagnose(ctx, app, "deploy failed")
    else:
        await ctx.step(f"سایت بالا آمد: {app_url(app)}", "done")
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
