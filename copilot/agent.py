"""Conversational agent with Human-in-the-Loop tool execution.

Two brains share the same tools and the same safety policy:
  * LLMBrain     – any OpenAI-compatible model (Liara AI, OpenAI, OpenRouter...) with tool calling
  * RuleBrain    – deterministic Persian intent router; works with no LLM key at all
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from . import config
from .liara_client import LiaraError, Limitation, app_url
from .policy import ApprovalRequest, Risk, audit, needs_approval
from .tools import REGISTRY, Ctx

SYSTEM_PROMPT = """تو «لیارا کوپایلوت» هستی؛ همکار فنی (DevOps + SRE) برای صاحبان وب‌سایت روی پلتفرم ابری لیارا.
کاربر ممکن است هیچ دانشی از دیپلوی و سرور نداشته باشد. همیشه فارسی، ساده، کوتاه و بدون اصطلاحات غیرضروری جواب بده.

اصول کار:
1. داده نساز. هر عددی، وضعیتی یا لاگی را فقط از خروجی ابزارها بگو.
2. انتشار (دیپلوی): اول analyze_project، بعد خلاصه‌ی نتیجه (نوع پروژه، پلن پیشنهادی با دلیل، متغیرهای لازم) را بگو و سپس deploy را صدا بزن. تأیید کاربر خودکار توسط سیستم گرفته می‌شود؛ خودت دوباره نپرس.
3. مشکل (کندی، خطای ۵۰۰، بالا نیامدن): همیشه اول diagnose. علت‌ها را به ترتیب احتمال با «شاهد» توضیح بده. سپس اولین راه‌حل علت اول را اجرا کن (سیستم تأیید می‌گیرد) و بلافاصله بعدش verify_fix را صدا بزن و نتیجه را قبل/بعد گزارش بده. اگر حل نشد، سراغ علت بعدی برو.
4. اگر راه‌حل موقت است (مثل ری‌استارت برای نشتی حافظه)، صریح بگو و راه‌حل دائمی در کد (فایل و خط) را هم بگو.
5. اگر ابزاری limitation برگرداند، محدودیت API لیارا را شفاف به کاربر اعلام کن و کاری که خودش باید انجام دهد را قدم‌به‌قدم بگو (مثل شارژ حساب، تنظیم DNS در ایرنیک، تغییر پلن از پنل).
6. اگر کاربر عملیاتی را رد کرد، احترام بگذار و گزینه‌ی جایگزین بده.
7. هیچ‌وقت مقدار متغیرهای محیطی یا توکن را در پاسخ تکرار نکن. برای deploy مقدار env نساز و حدس نزن: سیستم خودش
   متغیرهای لازم را از کد و فایل ‎.env‎ پروژه پیدا می‌کند و در کارت تأیید از کاربر می‌پرسد. فقط مقدارهایی که خود کاربر گفته را در envs بفرست.
8. اگر نام اپ مشخص نیست و چند اپ وجود دارد، با list_apps پیدا کن یا کوتاه بپرس.
9. دو نوع سرویس داری: «اپ» (PaaS؛ برای سایت و برنامه، زیرساخت با لیارا) و «سرور مجازی» (IaaS؛ لینوکس کامل با دسترسی root).
   برای سایت/API معمولاً اپ بهتر است؛ اگر کاربر سرور، VPS، ربات دائمی، VPN، یا کنترل کامل سیستم‌عامل خواست، سرور مجازی پیشنهاد بده.
10. ساخت اپ: فقط پلن‌های نسل g2 قابل ساخت‌اند (list_plans همین‌ها را می‌دهد). بسته‌ی امکانات و شبکه‌ی خصوصی خودکار انتخاب می‌شوند.
    اگر ساخت خطا داد، پیام دقیق لیارا را به کاربر بگو؛ هرگز حدس نزن که «API اجازه نمی‌دهد».
11. سرور مجازی: اول vm_options، بعد پلن و سیستم‌عامل را با دلیل پیشنهاد بده، سپس create_vm. بعد از ساخت، برای نصب نرم‌افزار
    از vm_setup با دستورالعمل‌های آماده (update, security, docker, nginx, node, python, postgres, swap) استفاده کن.
    رمز root را هرگز در پاسخ ننویس؛ رابط کاربری آن را جداگانه نشان می‌دهد.
12. تغییر منابع اپ با change_plan و سرور با vm_resize ممکن است (با تأیید). برای پیشنهاد پلن از suggest_plan کمک بگیر.
قالب پاسخ: چند خط کوتاه، در صورت نیاز فهرست. از جدول‌های بزرگ پرهیز کن؛ رابط کاربری کارت‌های تشخیص را خودش نمایش می‌دهد."""


AUTO_RESULT = {"list_apps", "list_plans", "list_vms", "get_logs", "vm_options", "domain_dns_guide", "health_check_all"}


def _mask(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k.startswith("_"):
            continue
        if k in ("variables", "envs") and isinstance(v, dict):
            out[k] = {kk: ("<باید وارد شود>" if str(vv).startswith("<") else "••••") for kk, vv in v.items()}
        else:
            out[k] = v
    return out


class Session:
    def __init__(self, client, sid: str | None = None):
        self.id = sid or uuid.uuid4().hex[:12]
        self.client = client
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, tuple[ApprovalRequest, asyncio.Future]] = {}
        self.state: dict = {}
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.busy = False
        self.brain = LLMBrain(self) if config.LLM_API_KEY else RuleBrain(self)

    # ------------------------------------------------------------ plumbing
    async def emit(self, ev: dict):
        await self.queue.put(ev)

    async def say(self, text: str):
        await self.emit({"type": "message", "role": "assistant", "text": text})

    def ctx(self) -> Ctx:
        return Ctx(self.client, self.emit, self.state)

    def resolve(self, approval_id: str, approved: bool, values: dict | None = None) -> bool:
        item = self.pending.pop(approval_id, None)
        if not item:
            return False
        req, fut = item
        if not fut.done():
            fut.set_result((approved, values or {}))
        return True

    async def execute(self, name: str, args: dict) -> Any:
        """Run a tool through the safety policy. Returns a JSON-able result."""
        t = REGISTRY.get(name)
        if not t:
            return {"error": f"ابزار ناشناخته: {name}"}
        args = {k: v for k, v in (args or {}).items() if v is not None}
        if t.prepare:
            try:
                args = await t.prepare(self.ctx(), args)
            except Exception as e:  # noqa
                return {"error": f"آماده‌سازی {name} ناموفق بود: {type(e).__name__}: {e}"}
        if needs_approval(t.risk):
            title, impact, rollback = t.approval(args) if t.approval else (name, "", "")
            inputs, target = [], ""
            meta = args.get("_env_meta") or {}
            for key in ("variables", "envs"):
                if isinstance(args.get(key), dict):
                    target = key
                    inputs = [{"key": k,
                               "required": meta.get(k, {}).get("required", str(v).startswith("<") or v in ("", None)),
                               "hint": meta.get(k, {}).get("hint", "مقدار را وارد کنید" if str(v).startswith("<") else "")}
                              for k, v in args[key].items()]
            req = ApprovalRequest(uuid.uuid4().hex[:10], name, _mask(args), t.risk, title, impact, rollback,
                                  inputs=inputs, input_target=target)
            fut = asyncio.get_running_loop().create_future()
            self.pending[req.id] = (req, fut)
            await self.emit({"type": "approval", **req.to_dict()})
            audit("approval_requested", tool=name, args=args, risk=t.risk.value)
            try:
                approved, values = await asyncio.wait_for(fut, timeout=900)
            except asyncio.TimeoutError:
                approved, values = False, {}
            audit("approval_" + ("granted" if approved else "denied"), tool=name)
            await self.emit({"type": "approval_result", "id": req.id, "approved": approved})
            if not approved:
                return {"rejected": True, "message": "کاربر این عملیات را تأیید نکرد."}
            if target and values:
                args[target] = {**args.get(target, {}), **{k: v for k, v in values.items() if v != ""}}
            if target and any(str(v).startswith("<") for v in args.get(target, {}).values()):
                return {"error": "مقدار برخی متغیرها وارد نشد؛ عملیات انجام نشد."}
        await self.emit({"type": "tool", "name": name, "status": "running", "risk": t.risk.value,
                         "args": _mask(args)})
        try:
            res = await t.fn(self.ctx(), **{k: v for k, v in args.items() if not k.startswith("_")})
            status = "done"
        except Limitation as e:
            res, status = {"limitation": str(e)}, "warn"
        except LiaraError as e:
            res, status = {"error": e.human(), "status": e.status}, "error"
        except Exception as e:  # noqa
            res, status = {"error": f"{type(e).__name__}: {e}"}, "error"
        audit("tool_executed", tool=name, args=args, risk=t.risk.value, status=status)
        await self.emit({"type": "tool", "name": name, "status": status})
        if isinstance(res, dict) and res.get("limitation"):
            await self.emit({"type": "limitation", "text": res["limitation"]})
        if name in AUTO_RESULT and status == "done":
            await self.emit({"type": "result", "tool": name, "data": res})
        if status == "error" and isinstance(res, dict):
            await self.emit({"type": "error", "tool": name, "text": res.get("error")})
        return res

    async def handle(self, text: str):
        if self.busy:
            await self.say("یک عملیات در حال انجام است؛ لطفاً چند لحظه صبر کنید.")
            return
        self.busy = True
        await self.emit({"type": "busy", "value": True})
        try:
            await self.brain.turn(text)
        except Exception as e:  # noqa
            await self.say(f"⚠️ خطای غیرمنتظره: {e}")
        finally:
            self.busy = False
            await self.emit({"type": "busy", "value": False})


# ===================================================================== LLM
class LLMBrain:
    def __init__(self, s: Session):
        from openai import AsyncOpenAI
        self.s = s
        self.llm = AsyncOpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL or None)
        self.tools = [t.schema() for t in REGISTRY.values()]
        self.no_temp = False

    async def turn(self, text: str):
        s = self.s
        s.history.append({"role": "user", "content": text})
        for _ in range(14):
            await s.emit({"type": "thinking"})
            try:
                kw = {} if self.no_temp else {"temperature": 0.2}
                resp = await self.llm.chat.completions.create(model=config.LLM_MODEL, messages=s.history,
                                                              tools=self.tools, **kw)
            except Exception as e:  # noqa
                if "temperature" in str(e) and not self.no_temp:
                    self.no_temp = True
                    continue
                s.history.pop() if s.history and s.history[-1].get("role") == "user" else None
                await s.emit({"type": "limitation", "text": f"مدل زبانی در دسترس نبود ({type(e).__name__})؛ "
                                                            "این درخواست با موتور قواعد داخلی انجام می‌شود."})
                return await RuleBrain(s).turn(text)
            msg = resp.choices[0].message
            entry = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in msg.tool_calls]
            s.history.append(entry)
            if msg.content and msg.content.strip():
                await s.say(msg.content)
            if not msg.tool_calls:
                return
            for c in msg.tool_calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                res = await s.execute(c.function.name, args)
                s.history.append({"role": "tool", "tool_call_id": c.id,
                                  "content": json.dumps(res, ensure_ascii=False, default=str)[:12000]})
        await s.say("به سقف مراحل این درخواست رسیدم؛ اگر لازم است ادامه بدهم بگویید.")


# =========================================================== deterministic
URL_RE = re.compile(r"(https?://\S+|git@\S+)")
PATH_RE = re.compile(r"((?:~|\.{1,2})?/[^\s،]+)")
DOMAIN_RE = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:ir|com|net|org|io|dev|app|co))\b", re.I)


TOOL_FA = {"restart_app": "ری‌استارت اپ", "set_env": "تنظیم متغیر محیطی", "rollback": "بازگشت به نسخه‌ی سالم قبلی",
           "update_port": "اصلاح پورت", "start_app": "روشن کردن اپ", "deploy": "انتشار",
           "change_plan": "ارتقای پلن"}


def _has(text, *words):
    t = text.lower()
    return any(w in t for w in words)


class RuleBrain:
    """No-LLM fallback: same tools, same policy, Persian intent routing."""

    def __init__(self, s: Session):
        self.s = s

    async def _app_from(self, text: str) -> str | None:
        apps = [a.get("project_id") or a.get("name") for a in await self.s.client.list_apps()]
        for a in sorted(apps, key=len, reverse=True):
            if a and a.lower() in text.lower():
                return a
        if self.s.state.get("current_app") in apps:
            return self.s.state["current_app"]
        return apps[0] if len(apps) == 1 else None

    async def turn(self, text: str):
        s = self.s
        src = (URL_RE.search(text) or PATH_RE.search(text))
        if src and _has(text, "دیپلوی", "deploy", "منتشر", "بالا بیار", "راه بنداز", "راه‌اندازی", "انتشار", "آپلود"):
            return await self.deploy_flow(src.group(1), text)
        if _has(text, "سرور", "vps", "vm", "ماشین مجازی", "virtual machine"):
            return await self.vm_flow(text)
        if _has(text, "همه", "all") and _has(text, "سلامت", "health", "وضعیت", "پایش"):
            return await self.health_all()
        app = await self._app_from(text)
        if app and _has(text, "پلن", "plan", "منابع") and _has(text, "تغییر", "ارتقا", "بزرگ", "کوچک", "upgrade", "افزایش", "کاهش"):
            return await self.plan_flow(app, text)
        if _has(text, "کند", "خطا", "۵۰۰", "500", "502", "ارور", "error", "مشکل", "بالا نمیاد", "قطع", "خراب", "slow", "down"):
            if not app:
                return await s.say("کدام اپ مشکل دارد؟ نام آن را بنویسید.")
            return await self.sre_flow(app, text)
        if _has(text, "لاگ", "log"):
            return await self.simple("get_logs", {"app": app}, app)
        if _has(text, "دامنه", "domain") and DOMAIN_RE.search(text):
            return await self.domain_flow(app, DOMAIN_RE.search(text).group(1))
        if _has(text, "پلن", "plan", "قیمت"):
            return await self.simple("list_plans", {}, None)
        if _has(text, "ری‌استارت", "ریستارت", "restart"):
            return await self.simple("restart_app", {"app": app}, app)
        if _has(text, "وضعیت", "سلامت", "status", "چطوره", "health"):
            if not app:
                return await self.health_all()
            return await self.status(app)
        if _has(text, "اپ", "app", "پروژه‌ها", "لیست"):
            return await self.simple("list_apps", {}, None)
        await s.say("می‌توانم این کارها را انجام دهم:\n"
                    "• «پروژه‌ی ‎/path/to/app‎ را دیپلوی کن» یا لینک گیت‌هاب\n"
                    "• «سایت shop-demo کند شده» / «خطای ۵۰۰ می‌دهد» → عیب‌یابی کامل\n"
                    "• «وضعیت همه‌ی اپ‌ها» • «لاگ‌های shop-demo» • «دامنه‌ی example.ir را وصل کن»")

    async def simple(self, tool, args, app):
        if app is None and "app" in REGISTRY[tool].required:
            return await self.s.say("نام اپ را بنویسید.")
        res = await self.s.execute(tool, args)
        await self.s.say("✅ انجام شد." if not (isinstance(res, dict) and (res.get("error") or res.get("rejected")))
                         else f"⚠️ {res.get('error') or res.get('message')}")

    async def status(self, app):
        s = self.s
        st = await s.execute("app_status", {"app": app})
        if st.get("error"):
            return await s.say(f"⚠️ {st['error']}")
        h = st["http_check"]
        icon = "🟢" if h.get("ok") else "🔴"
        await s.say(f"{icon} **{app}** — وضعیت {st['status']}، پلن {st['plan']}، پاسخ‌دهی {h.get('median_ms')}ms "
                    f"(کدها: {h.get('statuses')})\nآخرین ریلیز: {st['last_releases'][0]['state'] if st['last_releases'] else '—'}")

    async def health_all(self):
        res = await self.s.execute("health_check_all", {})
        bad = [r for r in res if r["top_issue"]] if isinstance(res, list) else []
        await self.s.say("همه‌ی اپ‌ها سالم هستند ✅" if not bad else
                         "اپ‌های نیازمند توجه:\n" + "\n".join(f"• {r['app']}: {r['top_issue']}" for r in bad)
                         + "\nبرای بررسی دقیق بنویسید: «مشکل ‎<نام اپ>‎ را پیدا کن»")

    async def sre_flow(self, app, symptom):
        s = self.s
        s.state["current_app"] = app
        rep = await s.execute("diagnose", {"app": app, "symptom": symptom})
        if rep.get("error"):
            return await s.say(f"⚠️ {rep['error']}")
        hyps = rep["hypotheses"]
        if not hyps:
            p = rep["summary"]["probe"]
            return await s.say(f"سایت سالم پاسخ می‌دهد ({p.get('median_ms')}ms، کدهای {p.get('statuses')}) و در لاگ‌ها "
                               "خطایی نیست. اگر مشکل مقطعی است، دقیق‌تر بگویید کدام صفحه و چه زمانی.")
        top = hyps[0]
        lines = [f"**تشخیص اصلی:** {top['title']} (اطمینان ٪{int(top['confidence'] * 100)})", top["explain"]]
        if len(hyps) > 1:
            lines.append("علت‌های دیگر: " + "، ".join(h["title"] for h in hyps[1:3]))
        await s.say("\n".join(lines))
        for h in hyps[:2]:
            resolved = []
            for f in h["fixes"]:
                if f["tool"] == "suggest_plan":
                    sp = await s.execute("suggest_plan", f["args"])
                    sug = sp.get("suggested") if isinstance(sp, dict) else None
                    if sug:
                        resolved.append({"tool": "change_plan", "args": {"app": f["args"]["app"], "plan_id": sug["id"]},
                                         "note": f"{f.get('note', '')} → {sug['id']} ({sug['ram_gb']}GB RAM)"})
                else:
                    resolved.append(f)
            fixes = [f for f in resolved if REGISTRY[f["tool"]].risk != Risk.READ]
            if not fixes:
                if h.get("permanent_fix"):
                    await s.say(f"🛠 راه‌حل برای «{h['title']}»: {h['permanent_fix']}")
                continue
            fix = fixes[0]
            await s.say(f"پیشنهاد من برای «{h['title']}»: **{TOOL_FA.get(fix['tool'], fix['tool'])}**"
                        + (f" — {fix['note']}" if fix.get("note") else "") + ". منتظر تأیید شما هستم 👇")
            res = await s.execute(fix["tool"], fix["args"])
            if isinstance(res, dict) and res.get("rejected"):
                await s.say("باشه، کاری انجام ندادم. هر وقت خواستید دوباره بگویید.")
                return
            if isinstance(res, dict) and res.get("limitation"):
                await s.say(f"ℹ️ {res['limitation']}")
                continue
            ver = await s.execute("verify_fix", {"app": app})
            b, a = ver.get("before", {}), ver.get("after", {})
            if ver.get("fixed"):
                msg = (f"✅ مشکل برطرف شد. قبل: کدها {b.get('statuses')} / {b.get('median_ms')}ms ← "
                       f"بعد: کدها {a.get('statuses')} / {a.get('median_ms')}ms")
                if h.get("permanent_fix"):
                    msg += f"\n⚠️ این راه‌حل {'موقت' if h['key'] == 'memory' else 'اعمال شد'}؛ برای جلوگیری از تکرار: {h['permanent_fix']}"
                for c in h.get("code") or []:
                    msg += f"\n📍 ‎{c['file']}:{c['line']}‎" + (f" — {c['url']}" if c.get("url") else "")
                return await s.say(msg)
            await s.say(f"هنوز کامل حل نشده (بعد: {a.get('statuses')} / {a.get('median_ms')}ms). علت بعدی را بررسی می‌کنم…")
        await s.say("برای ادامه، لاگ‌های کامل را ببینید یا جزئیات بیشتری از مشکل بدهید.")

    async def plan_flow(self, app, text):
        s = self.s
        down = _has(text, "کوچک", "کاهش", "ارزان", "down")
        sp = await s.execute("suggest_plan", {"app": app, "direction": "down" if down else "up"})
        sug = sp.get("suggested") if isinstance(sp, dict) else None
        if not sug:
            return await s.say(f"پلن مناسب‌تری از {sp.get('current_plan')} پیدا نکردم.")
        await s.say(f"پلن فعلی **{sp['current_plan']}** ({sp['current_ram_gb']}GB RAM) است. پیشنهاد: **{sug['id']}** — "
                    f"{sug['ram_gb']}GB RAM، {sug['cpu']} هسته، حدود {int(sug['price']):,} تومان در ماه. منتظر تأیید شما 👇")
        res = await s.execute("change_plan", {"app": app, "plan_id": sug["id"]})
        if res.get("ok"):
            await s.say(f"✅ پلن «{app}» به {sug['id']} تغییر کرد. وضعیت را بررسی می‌کنم…")
            await self.status(app)
        elif not res.get("rejected"):
            await s.say(f"⚠️ {res.get('error')}")

    async def vm_flow(self, text):
        s = self.s
        t = text.lower()
        if _has(text, "لیست", "کدام", "list", "سرورهام", "سرورهای من") and not _has(text, "بساز", "ایجاد", "بگیر", "create"):
            res = await s.execute("list_vms", {})
            return await s.say("سرور مجازی‌ای ندارید." if not res else f"{len(res)} سرور مجازی دارید.")
        vms = await s.client.vm_list() if hasattr(s.client, "vm_list") else []
        named = next((v["name"] for v in vms if v["name"] in t), None)
        recipes = [r for r, words in {"docker": ("docker", "داکر"), "nginx": ("nginx", "انجینکس", "وب‌سرور", "وب سرور"),
                                      "node": ("node", "نود"), "python": ("python", "پایتون"),
                                      "postgres": ("postgres", "پستگرس", "دیتابیس"), "security": ("امن", "فایروال", "security"),
                                      "swap": ("swap", "سواپ"), "update": ("آپدیت", "update")}.items()
                   if any(w in t for w in words)]
        if named and _has(text, "سلامت", "وضعیت", "health"):
            info = await s.execute("vm_info", {"vm": named})
            r = await s.execute("vm_ssh_run", {"vm": named, "recipe": "health"})
            return await s.say("✅ گزارش سلامت سرور گرفته شد." if r.get("ok") else f"⚠️ {r.get('error')}")
        if named and recipes:
            return await self._vm_setup(named, recipes)
        if named and _has(text, "خاموش", "stop", "shutdown", "روشن", "start", "ری‌استارت", "ریستارت", "reboot"):
            action = "start" if _has(text, "روشن", "start") else "reboot" if _has(text, "ری‌استارت", "ریستارت", "reboot") else "shutdown"
            r = await s.execute("vm_power", {"vm": named, "action": action})
            return await s.say("✅ انجام شد." if r.get("ok") else f"⚠️ {r.get('error') or r.get('message')}")
        if named:
            await s.execute("vm_info", {"vm": named})
            return await s.say(f"اطلاعات سرور «{named}» را نشان دادم. می‌توانم روی آن docker، nginx، node، python، postgres، "
                               "امن‌سازی یا swap نصب کنم؛ کافی است بگویید.")
        wants_create = _has(text, "بساز", "ایجاد", "بگیر", "راه بنداز", "create", "می‌خوام", "میخوام", "لازم دارم")
        if not wants_create:
            opts = await s.execute("vm_options", {})
            return await s.say("پلن‌ها و سیستم‌عامل‌های سرور مجازی لیارا را آوردم. برای ساخت بنویسید مثلاً "
                               "«یک سرور مجازی بساز اسم my-server و روش docker نصب کن».")
        # ---- create a new VM
        opts = await s.execute("vm_options", {})
        if opts.get("error"):
            return await s.say(f"⚠️ {opts['error']}")
        plans = opts["plans"]
        heavy = _has(text, "قوی", "سنگین", "دیتابیس", "زیاد", "pro")
        plan = (plans[-2] if heavy and len(plans) > 1 else plans[0]) if plans else None
        os_ = next((o for o in opts["os"] if "ubuntu-24" in o), opts["os"][0] if opts["os"] else "ubuntu-24.04")
        m = re.search(r"(?:اسم|نام|name)\s*[:=]?\s*([a-z][a-z0-9-]{3,18})", text, re.I)
        name = m.group(1).lower() if m else f"server-{uuid.uuid4().hex[:4]}"
        purpose = "، ".join({"docker": "Docker", "nginx": "وب‌سرور", "node": "Node.js", "python": "Python",
                             "postgres": "PostgreSQL", "security": "امن‌سازی", "swap": "Swap", "update": "به‌روزرسانی"}[r]
                            for r in recipes)
        await s.say(f"🖥 برای شما یک سرور مجازی می‌سازم:\n"
                    f"• پلن پیشنهادی: **{plan['id']}** — {plan['cpu']} هسته، {plan['ram_gb']}GB RAM، {plan['disk_gb']}GB دیسک، "
                    f"حدود {int(plan['monthly_price']):,} تومان در ماه"
                    + (" (برای کار سنگین)" if heavy else " (ارزان‌ترین گزینه‌ی کافی برای شروع)")
                    + f"\n• سیستم‌عامل: **{os_}** (پایدار و پرکاربرد)"
                    + (f"\n• بعد از ساخت نصب می‌کنم: {purpose}" if recipes else "")
                    + "\nمنتظر تأیید شما 👇")
        res = await s.execute("create_vm", {"name": name, "plan": plan["id"], "os": os_})
        if res.get("rejected"):
            return await s.say("باشه، سروری ساخته نشد.")
        if not res.get("created"):
            return await s.say(f"⚠️ {res.get('error') or 'ساخت سرور هنوز تمام نشده؛ بعداً وضعیت را بپرسید.'}")
        await s.say(f"✅ سرور «{name}» آماده است. IP: ‎{res.get('ip')}‎ — رمز root در کارت سرور نمایش داده شده.")
        if recipes:
            await self._vm_setup(name, ["update"] + [r for r in recipes if r != "update"])

    async def _vm_setup(self, vm, recipes):
        s = self.s
        await s.say("🛠 پیکربندی سرور: " + "، ".join(recipes) + ". دستورات دقیق در کارت تأیید آمده 👇")
        r = await s.execute("vm_setup", {"vm": vm, "recipes": recipes})
        if r.get("rejected"):
            return await s.say("باشه، چیزی روی سرور اجرا نشد.")
        await s.say("✅ پیکربندی با موفقیت انجام شد." if r.get("ok") else f"⚠️ {r.get('error') or 'بخشی از دستورات ناموفق بود؛ جزئیات در تایم‌لاین است.'}")

    async def deploy_flow(self, source, text):
        s = self.s
        an = await s.execute("analyze_project", {"source": source})
        if an.get("error"):
            return await s.say(f"⚠️ {an['error']}")
        plan = an.get("plan") or {}
        envs = list(an["env_vars"].keys())
        msg = [f"📦 پروژه‌ی شما **{an['platform_fa']}** است ({'، '.join(an['reasons'])}).",
               f"🔌 پورت: {an['port']} (از {an['port_source']})",
               f"💡 پلن پیشنهادی: **{plan.get('id')}** — {plan.get('ram_gb')}GB RAM؛ دلیل: {an['needs']['reason']}"]
        if plan.get("alternative"):
            msg.append(f"   (اگر ترافیک زیاد دارید: {plan['alternative']['id']})")
        if envs:
            msg.append(f"🔑 برنامه به این متغیرها نیاز دارد: {', '.join(envs)} — در کارت تأیید مقدارشان را وارد کنید.")
        for w in an["warnings"]:
            msg.append(f"⚠️ {w}")
        await s.say("\n".join(msg))
        name = None
        m = re.search(r"(?:اسم|نام|name)\s*[:=]?\s*([a-z][a-z0-9-]{2,30})", text, re.I)
        if m:
            name = m.group(1).lower()
        if not name:
            base = re.sub(r"[^a-z0-9-]", "-", an["path"].rstrip("/").split("/")[-1].lower()).strip("-")[:20] or "app"
            name = f"{base}-{uuid.uuid4().hex[:4]}"
        exists = name in [a.get("project_id") or a.get("name") for a in await s.client.list_apps()]
        res = await s.execute("deploy", {"source": source, "app": name, "create": not exists,
                                         "plan_id": plan.get("id"), "platform": an["platform"], "port": an["port"],
                                         "_analysis": an})
        if isinstance(res, dict) and res.get("rejected"):
            return await s.say("انتشار لغو شد. اگر پلن یا نام دیگری می‌خواهید بگویید.")
        if isinstance(res, dict) and res.get("state") == "READY":
            s.state["current_app"] = name
            return await s.say(f"🚀 سایت شما آنلاین است: {res['url']}\n"
                               f"قدم بعدی: برای اتصال دامنه بنویسید «دامنه‌ی mysite.ir را به {name} وصل کن».")
        await s.say(f"⚠️ انتشار کامل نشد: {res.get('error') or res.get('state')}")

    async def domain_flow(self, app, domain):
        s = self.s
        if not app:
            return await s.say("دامنه را به کدام اپ وصل کنم؟")
        r = await s.execute("add_domain", {"app": app, "domain": domain})
        g = await s.execute("domain_dns_guide", {"app": app, "domain": domain})
        a = g["option_A_recommended"]
        await s.say((f"✅ دامنه در لیارا ثبت شد.\n" if not r.get("error") else f"⚠️ {r['error']}\n")
                    + f"**کارهایی که فقط از دست شما برمی‌آید ({'ایرنیک' if domain.endswith('.ir') else 'ثبت‌کننده‌ی دامنه'}):**\n"
                    + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(a["steps"]))
                    + f"\nیا رکورد CNAME → ‎{app}.liara.run‎. بعد از اعمال، بگویید «دامنه را چک کن».")
