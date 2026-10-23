"""IaaS tools: Liara virtual machines (create / power / resize / password / delete)
and in-server configuration over SSH with ready-made, reviewable setup recipes.

API: developers.liara.ir/pages/iaas.md  (base https://iaas-api.liara.ir)
"""
from __future__ import annotations

import asyncio
import re
import time

from .liara_client import LiaraError, plan_price_month
from .policy import Risk
from .tools import Ctx, tool

VM = {"type": "string", "description": "نام یا شناسه‌ی سرور مجازی"}

# ----------------------------------------------------------- setup recipes
# Every recipe is a plain list of shell commands, shown to the user in the
# approval card before anything runs on the server.
RECIPES: dict[str, dict] = {
    "update": {"title": "به‌روزرسانی سیستم‌عامل", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get upgrade -y",
    ]},
    "security": {"title": "امن‌سازی پایه (فایروال + fail2ban + آپدیت خودکار)", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y ufw fail2ban unattended-upgrades",
        "ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp",
        "ufw --force enable",
        "systemctl enable --now fail2ban",
    ]},
    "docker": {"title": "نصب Docker و Docker Compose", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y docker.io docker-compose-v2 || apt-get install -y docker.io docker-compose",
        "systemctl enable --now docker",
        "docker --version",
    ]},
    "nginx": {"title": "نصب وب‌سرور Nginx", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y nginx",
        "systemctl enable --now nginx",
        "(command -v ufw >/dev/null && ufw allow 'Nginx Full') || true",
        "curl -s -o /dev/null -w 'nginx http %{http_code}\\n' http://localhost",
    ]},
    "node": {"title": "نصب Node.js و PM2", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y nodejs npm",
        "npm install -g pm2",
        "node --version && pm2 --version",
    ]},
    "python": {"title": "نصب Python، pip و venv", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y python3 python3-pip python3-venv",
        "python3 --version",
    ]},
    "postgres": {"title": "نصب دیتابیس PostgreSQL", "cmds": [
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y",
        "apt-get install -y postgresql",
        "systemctl enable --now postgresql",
        "sudo -u postgres psql -c 'select version();'",
    ]},
    "swap": {"title": "ساخت فایل Swap دو گیگابایتی (جلوگیری از کمبود RAM)", "cmds": [
        "test -f /swapfile || (fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile)",
        "swapon /swapfile || true",
        "grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab",
        "free -h",
    ]},
    "health": {"title": "گزارش سلامت سرور (فقط خواندنی)", "cmds": [
        "uptime", "free -h", "df -h /", "top -bn1 | head -15",
        "systemctl --failed --no-pager || true",
    ], "read_only": True},
}

BLOCKED = [r"\brm\s+-rf\s+/(\s|$)", r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{", r"\bshutdown\b", r"\breboot\b"]


def _vm_summary(v: dict) -> dict:
    plan = v.get("planDetails") or {}
    return {
        "id": v.get("_id"), "name": v.get("name"), "state": v.get("state"), "power": v.get("power"),
        "os": v.get("OS"), "plan": v.get("plan"),
        "ip": ", ".join(ip.get("address", "") for ip in v.get("IPs", []) if ip.get("version") in (None, "v4", "4", 4))
              or ", ".join(ip.get("address", "") for ip in v.get("IPs", [])),
        "cpu": (plan.get("CPU") or {}).get("amount"), "ram_gb": (plan.get("RAM") or {}).get("amount"),
        "disk_gb": plan.get("volume"), "monthly_price": plan.get("monthlyPrice"),
        "hostname": (v.get("config") or {}).get("hostname"),
    }


async def _resolve(ctx: Ctx, vm: str) -> dict:
    v = await ctx.client.vm_find(vm)
    try:
        full = await ctx.client.vm_get(v["_id"])
        return full or v
    except LiaraError:
        return v


def _plan_rows(plans: dict) -> list[dict]:
    rows = []
    for pid, p in (plans or {}).items():
        if not isinstance(p, dict) or p.get("available") is False:
            continue
        rows.append({"id": pid, "cpu": (p.get("CPU") or {}).get("amount"), "ram_gb": (p.get("RAM") or {}).get("amount"),
                     "disk_gb": p.get("volume"), "monthly_price": plan_price_month(p)})
    return sorted(rows, key=lambda r: (r["ram_gb"] or 0, r["monthly_price"]))


# ================================================================== READ
@tool("list_vms", "لیست سرورهای مجازی (IaaS) حساب لیارا با وضعیت، IP و مشخصات.")
async def list_vms(ctx: Ctx):
    out = []
    for v in await ctx.client.vm_list():
        try:
            out.append(_vm_summary(await ctx.client.vm_get(v["_id"])))
        except LiaraError:
            out.append(_vm_summary(v))
    return out


@tool("vm_options", "پلن‌ها (CPU/RAM/دیسک/قیمت ماهانه) و سیستم‌عامل‌های قابل‌انتخاب برای ساخت سرور مجازی لیارا.")
async def vm_options(ctx: Ctx):
    plans, oss = await asyncio.gather(ctx.client.vm_plans(), ctx.client.vm_os_list())
    os_list = [f"{name}-{ver}" for name, vers in (oss or {}).items() if isinstance(vers, list) for ver in vers]
    return {"plans": _plan_rows(plans), "os": os_list,
            "recipes": {k: v["title"] for k, v in RECIPES.items()}}


@tool("vm_info", "جزئیات یک سرور مجازی: وضعیت، IP، مشخصات و آخرین عملیات‌ها.", {"vm": VM}, required=("vm",))
async def vm_info(ctx: Ctx, vm: str):
    v = await _resolve(ctx, vm)
    ops = []
    try:
        ops = (await ctx.client.vm_operations(v["_id"]))[-5:]
    except LiaraError:
        pass
    pw = (v.get("config") or {}).get("rootPassword")
    if pw:
        ctx.state.setdefault("vm_passwords", {})[v["_id"]] = pw
    summ = _vm_summary(v)
    # the password goes only to the user's screen, never into the LLM context
    await ctx.emit({"type": "vm", "vm": summ, "root_password": pw})
    return {**summ, "operations": ops, "root_password": "•••• (فقط در کارت رابط کاربری نمایش داده می‌شود)" if pw else None}


@tool("vm_ssh_run", "اجرای یک دستور فقط‌خواندنی (مثل گزارش سلامت) روی سرور مجازی از طریق SSH.",
      {"vm": VM, "recipe": {"type": "string", "enum": ["health"]}}, required=("vm",))
async def vm_ssh_run(ctx: Ctx, vm: str, recipe: str = "health"):
    return await _ssh_recipe(ctx, vm, RECIPES["health"]["cmds"])


# ========================================================= WRITE (guarded)
def _create_approval(a):
    return (f"ساخت سرور مجازی «{a['name']}» با سیستم‌عامل {a.get('os', 'ubuntu-24.04')} و پلن {a['plan']}",
            "هزینه‌ی ماهانه‌ی پلن از اعتبار حساب لیارا کم می‌شود (ساعتی محاسبه می‌شود).",
            "با حذف سرور، هزینه متوقف می‌شود.")


@tool("create_vm",
      "ساخت سرور مجازی (IaaS) جدید در لیارا. قبلش vm_options را بگیر و پلن و سیستم‌عامل مناسب را با دلیل پیشنهاد بده. "
      "بعد از ساخت تا آماده شدن صبر می‌کند و IP را برمی‌گرداند.",
      {"name": {"type": "string", "description": "۴ تا ۱۹ کاراکتر، حروف کوچک انگلیسی، عدد و خط تیره"},
       "plan": {"type": "string", "description": "مثلاً standard-base-g2"},
       "os": {"type": "string", "description": "مثلاً ubuntu-24.04"},
       "ssh_key": {"type": "string", "description": "کلید عمومی SSH (اختیاری)"}},
      risk=Risk.HIGH, required=("name", "plan"), approval=_create_approval)
async def create_vm(ctx: Ctx, name: str, plan: str, os: str = "ubuntu-24.04", ssh_key: str | None = None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{3,18}", name):
        return {"error": "نام سرور باید ۴ تا ۱۹ کاراکتر باشد و با حرف کوچک انگلیسی شروع شود (فقط a-z، 0-9 و -)."}
    await ctx.step(f"ارسال درخواست ساخت سرور «{name}» ({plan}، {os})")
    res = await ctx.client.vm_create(name, os, plan, [ssh_key] if ssh_key else None)
    await ctx.step("درخواست ثبت شد؛ منتظر آماده شدن سرور (معمولاً ۱ تا ۳ دقیقه)", "done")
    await ctx.step("در حال ساخت سرور")
    t0, state = time.time(), ""
    while time.time() - t0 < 420:
        try:
            v = await ctx.client.vm_find(name)
            state = str(v.get("state", "")).upper()
            if state == "CREATED":
                break
        except LiaraError:
            pass
        await asyncio.sleep(0.2 if getattr(ctx.client, "is_mock", False) else 6)
    if state != "CREATED":
        await ctx.step("ساخت سرور بیش از حد معمول طول کشید؛ وضعیت را بعداً بررسی کنید", "warn")
        return {"task": res, "state": state or "PENDING"}
    await ctx.step("سرور ساخته شد ✓", "done")
    info = await vm_info(ctx, name)
    return {"created": True, **info}


@tool("vm_power", "روشن، خاموش، ری‌استارت یا shutdown امن سرور مجازی.",
      {"vm": VM, "action": {"type": "string", "enum": ["start", "stop", "reboot", "shutdown"]}},
      risk=Risk.HIGH, required=("vm", "action"),
      approval=lambda a: ({"start": f"روشن کردن سرور «{a['vm']}»", "stop": f"خاموش کردن فوری سرور «{a['vm']}»",
                           "reboot": f"ری‌استارت سرور «{a['vm']}»", "shutdown": f"خاموش کردن امن سرور «{a['vm']}»"}[a["action"]],
                          "سرویس‌های روی سرور تا پایان عملیات در دسترس نیستند." if a["action"] != "start" else "سرور بالا می‌آید.",
                          "با عملیات معکوس (روشن/خاموش)."))
async def vm_power(ctx: Ctx, vm: str, action: str):
    v = await ctx.client.vm_find(vm)
    await ctx.client.vm_power(v["_id"], action)
    return {"ok": True, "vm": v.get("name"), "action": action}


@tool("vm_resize", "تغییر پلن (CPU/RAM/دیسک) سرور مجازی.",
      {"vm": VM, "plan": {"type": "string"}}, risk=Risk.HIGH, required=("vm", "plan"),
      approval=lambda a: (f"تغییر پلن سرور «{a['vm']}» به {a['plan']}",
                          "سرور ممکن است برای اعمال تغییر ری‌استارت شود و هزینه‌ی ماهانه تغییر می‌کند.",
                          "با تغییر دوباره‌ی پلن (کوچک کردن دیسک ممکن نیست)."))
async def vm_resize(ctx: Ctx, vm: str, plan: str):
    v = await ctx.client.vm_find(vm)
    return {"ok": True, "api": await ctx.client.vm_resize(v["_id"], plan)}


@tool("vm_reset_password", "بازنشانی رمز root سرور مجازی (سرور باید خاموش باشد).", {"vm": VM},
      risk=Risk.HIGH, required=("vm",),
      approval=lambda a: (f"بازنشانی رمز root سرور «{a['vm']}»", "رمز قبلی دیگر کار نمی‌کند.",
                          "رمز جدید فقط در رابط کاربری به شما نشان داده می‌شود."))
async def vm_reset_password(ctx: Ctx, vm: str):
    v = await ctx.client.vm_find(vm)
    try:
        res = await ctx.client.vm_reset_password(v["_id"])
    except LiaraError as e:
        if e.status == 409:
            return {"error": "برای بازنشانی رمز، سرور باید خاموش باشد. اول با vm_power آن را shutdown کنید."}
        raise
    pw = res.get("password")
    if pw:
        ctx.state.setdefault("vm_passwords", {})[v["_id"]] = pw
        await ctx.emit({"type": "secret", "title": f"رمز جدید root سرور {v.get('name')}", "value": pw})
    return {"ok": True, "password": "•••• (در کارت رابط کاربری نمایش داده شد)"}


@tool("vm_setup",
      "پیکربندی نرم‌افزاری داخل سرور مجازی از طریق SSH با دستورالعمل‌های آماده: "
      "update, security, docker, nginx, node, python, postgres, swap. چند مورد را با هم می‌توان اجرا کرد. "
      "برای دستورات سفارشی از custom_commands استفاده کن؛ همه‌ی دستورات قبل از اجرا به کاربر نشان داده می‌شوند.",
      {"vm": VM, "recipes": {"type": "array", "items": {"type": "string"}},
       "custom_commands": {"type": "array", "items": {"type": "string"}}},
      risk=Risk.HIGH, required=("vm",),
      approval=lambda a: (f"پیکربندی سرور «{a['vm']}»: " + "، ".join(RECIPES[r]["title"] for r in a.get("recipes", []) if r in RECIPES)
                          + (" + دستورات سفارشی" if a.get("custom_commands") else ""),
                          "این دستورات با کاربر root روی سرور اجرا می‌شوند:\n" + "\n".join(_commands(a)),
                          "تغییرات نرم‌افزاری را باید دستی برگرداند؛ اسنپ‌شات قبل از کار توصیه می‌شود."))
async def vm_setup(ctx: Ctx, vm: str, recipes: list | None = None, custom_commands: list | None = None):
    cmds = _commands({"recipes": recipes or [], "custom_commands": custom_commands or []})
    for c in cmds:
        if any(re.search(p, c) for p in BLOCKED):
            return {"error": f"دستور «{c}» خطرناک تشخیص داده شد و اجرا نمی‌شود."}
    if not cmds:
        return {"error": "هیچ دستوری مشخص نشده است."}
    return await _ssh_recipe(ctx, vm, cmds)


@tool("delete_vm", "حذف کامل سرور مجازی و همه‌ی داده‌هایش (غیرقابل بازگشت).", {"vm": VM},
      risk=Risk.CRITICAL, required=("vm",),
      approval=lambda a: (f"حذف کامل سرور «{a['vm']}»", "دیسک و همه‌ی داده‌های سرور پاک می‌شوند.", "⛔ غیرقابل بازگشت است."))
async def delete_vm(ctx: Ctx, vm: str):
    v = await ctx.client.vm_find(vm)
    return {"ok": True, "api": await ctx.client.vm_delete(v["_id"])}


# ------------------------------------------------------------------- SSH
def _commands(a: dict) -> list[str]:
    out: list[str] = []
    for r in a.get("recipes") or []:
        if r in RECIPES:
            for c in RECIPES[r]["cmds"]:
                if c in out and (c.startswith("export") or c == "apt-get update -y"):
                    continue
                out.append(c)
    out += [c for c in (a.get("custom_commands") or []) if c.strip()]
    return out


async def _ssh_recipe(ctx: Ctx, vm: str, cmds: list[str]) -> dict:
    v = await _resolve(ctx, vm)
    summ = _vm_summary(v)
    ip = (summ["ip"] or "").split(",")[0].strip()
    pw = (v.get("config") or {}).get("rootPassword") or ctx.state.get("vm_passwords", {}).get(v.get("_id"))
    if getattr(ctx.client, "is_mock", False):
        return await ctx.client.mock_ssh(v, cmds, ctx)
    if not ip:
        return {"error": "IP سرور هنوز مشخص نیست؛ چند ثانیه بعد دوباره تلاش کنید."}
    if not pw:
        return {"error": "رمز root در دسترس نیست. با vm_reset_password رمز جدید بگیرید یا کلید SSH اضافه کنید."}
    try:
        import paramiko  # noqa
    except ImportError:
        return {"error": "کتابخانه‌ی paramiko نصب نیست: pip install paramiko"}
    await ctx.step(f"اتصال SSH به ‎root@{ip}‎")

    def run_all():
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(ip, username="root", password=pw, timeout=20, banner_timeout=20, auth_timeout=20)
        results, env = [], ""
        try:
            for c in cmds:
                if c.startswith("export "):
                    env += c + "; "
                    continue
                _, so, se = cli.exec_command(env + c, timeout=900, get_pty=False)
                code = so.channel.recv_exit_status()
                out = (so.read().decode(errors="ignore") + se.read().decode(errors="ignore")).strip()
                results.append({"cmd": c, "exit": code, "tail": out[-600:]})
                if code != 0:
                    break
        finally:
            cli.close()
        return results

    try:
        results = await asyncio.to_thread(run_all)
    except Exception as e:  # noqa
        await ctx.step(f"اتصال SSH ناموفق: {type(e).__name__}", "error")
        return {"error": f"اتصال SSH برقرار نشد ({type(e).__name__}: {str(e)[:150]}). "
                         "مطمئن شوید سرور روشن است و پورت ۲۲ باز است."}
    for r in results:
        await ctx.step(f"‎$ {r['cmd'][:70]}‎", "done" if r["exit"] == 0 else "error",
                       detail="\n".join(r["tail"].splitlines()[-4:]) or None)
    ok = all(r["exit"] == 0 for r in results) and len(results) == len([c for c in cmds if not c.startswith("export ")])
    return {"ok": ok, "ip": ip, "results": results}
