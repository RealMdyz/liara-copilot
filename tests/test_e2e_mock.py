"""End-to-end scenarios against the Liara simulator (no network, no LLM).

Run:  MOCK_MODE=1 python -m pytest -q tests
"""
import asyncio
import os
from pathlib import Path

os.environ["MOCK_MODE"] = "1"
os.environ["LLM_API_KEY"] = ""
os.environ["AUDIT_LOG_PATH"] = "/tmp/copilot-test-audit.jsonl"

from copilot.agent import Session  # noqa: E402
from copilot.mock_liara import MockLiara  # noqa: E402

VICTIM = str(Path(__file__).resolve().parents[1] / "victim-app")


async def run(session, text, approve=True, values=None):
    events = []
    task = asyncio.create_task(session.handle(text))
    while True:
        try:
            ev = await asyncio.wait_for(session.queue.get(), timeout=10)
        except asyncio.TimeoutError:
            break
        events.append(ev)
        if ev["type"] == "approval":
            session.resolve(ev["id"], approve, values)
        if ev["type"] == "busy" and ev["value"] is False:
            break
    await task
    return events


def msgs(evs):
    return "\n".join(e["text"] for e in evs if e["type"] == "message")


def test_deploy_flow():
    async def go():
        s = Session(MockLiara())
        evs = await run(s, f"پروژه‌ی {VICTIM} را دیپلوی کن", values={"DATABASE_URL": "postgres://u@h/db"})
        text = msgs(evs)
        assert "Node.js" in text
        assert any(e["type"] == "approval" and e["tool"] == "deploy" for e in evs)
        assert any(e["type"] == "approval" and e["inputs"] for e in evs), "env inputs expected"
        assert "آنلاین" in text, text
    asyncio.run(go())


def test_deploy_rejected():
    async def go():
        s = Session(MockLiara())
        evs = await run(s, f"پروژه‌ی {VICTIM} را دیپلوی کن", approve=False)
        assert "لغو" in msgs(evs)
    asyncio.run(go())


def test_500_missing_env_diagnose_fix_verify():
    async def go():
        c = MockLiara(); c.inject_fault("shop-demo", "missing_env")
        s = Session(c)
        evs = await run(s, "سایت shop-demo خطای ۵۰۰ میده", values={"DATABASE_URL": "postgres://x"})
        diag = next(e for e in evs if e["type"] == "diagnosis")["report"]
        assert diag["hypotheses"][0]["key"] == "missing_env"
        ap = next(e for e in evs if e["type"] == "approval")
        assert ap["tool"] == "set_env" and ap["inputs"][0]["required"]
        ver = next(e for e in evs if e["type"] == "verification")["result"]
        assert ver["fixed"] and "missing_env" in ver["resolved"]
        assert "برطرف" in msgs(evs)
    asyncio.run(go())


def test_slow_memory_leak():
    async def go():
        c = MockLiara(); c.inject_fault("shop-demo", "memory_leak")
        s = Session(c)
        evs = await run(s, "سایت shop-demo خیلی کند شده")
        diag = next(e for e in evs if e["type"] == "diagnosis")["report"]
        top = diag["hypotheses"][0]
        assert top["key"] == "memory" and top["code"], top
        assert next(e for e in evs if e["type"] == "approval")["tool"] == "restart_app"
        assert next(e for e in evs if e["type"] == "verification")["result"]["fixed"]
        assert "server.js" in msgs(evs)
    asyncio.run(go())


def test_bad_release_rollback():
    async def go():
        c = MockLiara(); c.inject_fault("shop-demo", "bad_release")
        s = Session(c)
        evs = await run(s, "بعد از آپدیت سایت shop-demo بالا نمیاد")
        assert next(e for e in evs if e["type"] == "approval")["tool"] == "rollback"
        assert next(e for e in evs if e["type"] == "verification")["result"]["fixed"]
    asyncio.run(go())


def test_reject_fix_does_nothing():
    async def go():
        c = MockLiara(); c.inject_fault("shop-demo", "memory_leak")
        s = Session(c)
        evs = await run(s, "shop-demo کنده", approve=False)
        assert not any(e["type"] == "verification" for e in evs)
        assert c.apps["shop-demo"]["fault"] == "memory_leak"
    asyncio.run(go())


def test_health_all_and_port():
    async def go():
        c = MockLiara(); c.inject_fault("shop-demo", "port"); c.apps["shop-demo"]["port"] = 3000
        c.apps["shop-demo"]["port"] = 3000
        s = Session(c)
        evs = await run(s, "وضعیت همه‌ی اپ‌ها")
        assert "shop-demo" in msgs(evs)
    asyncio.run(go())


def test_vm_create_and_setup():
    async def go():
        c = MockLiara()
        s = Session(c)
        evs = await run(s, "یک سرور مجازی بساز اسم web-box و روش docker و nginx نصب کن")
        tools = [e["tool"] for e in evs if e["type"] == "approval"]
        assert tools == ["create_vm", "vm_setup"], tools
        assert "web-box" in c.vms and c.vms["web-box"]["state"] == "CREATED"
        assert any(e["type"] == "vm" for e in evs)
        setup = next(e for e in evs if e["type"] == "approval" and e["tool"] == "vm_setup")
        assert "docker" in setup["impact"] and "nginx" in setup["impact"]
        assert "پیکربندی با موفقیت" in msgs(evs)
        # the root password must never reach chat text
        assert c.vms["web-box"]["config"]["rootPassword"] not in msgs(evs)
    asyncio.run(go())


def test_change_plan_flow():
    async def go():
        c = MockLiara()
        s = Session(c)
        evs = await run(s, "پلن shop-demo را ارتقا بده")
        ap = next(e for e in evs if e["type"] == "approval")
        assert ap["tool"] == "change_plan" and ap["args"]["plan_id"] == "medium-g2"
        assert c.apps["shop-demo"]["planID"] == "medium-g2"
    asyncio.run(go())


def test_create_app_rejects_old_plans():
    async def go():
        from copilot.tools import REGISTRY, Ctx
        c = MockLiara()
        async def emit(e): pass
        r = await REGISTRY["create_app"].fn(Ctx(c, emit, {}), name="x-app", platform="node", plan_id="small")
        assert "g2" in r["error"]
        r = await REGISTRY["create_app"].fn(Ctx(c, emit, {}), name="x-app", platform="node", plan_id="small-g2")
        assert r["created"] == "x-app"
    asyncio.run(go())


# ---------------------------------------------------------------- env vars
import json, shutil, tempfile, types  # noqa: E402


def _llm_session(client, script):
    """Session whose LLM is scripted: each item is (tool_name, args) or a final text."""
    os.environ["LLM_API_KEY"] = "x"
    from copilot import config
    config.LLM_API_KEY = "x"
    s = Session(client)
    config.LLM_API_KEY = ""
    os.environ["LLM_API_KEY"] = ""
    n = {"i": 0}

    async def create(**kw):
        st = script[min(n["i"], len(script) - 1)]
        n["i"] += 1
        if isinstance(st, str):
            m = types.SimpleNamespace(content=st, tool_calls=None)
        else:
            m = types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(
                id=f"c{n['i']}", function=types.SimpleNamespace(name=st[0], arguments=json.dumps(st[1])))])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m)])
    s.brain.llm.chat.completions.create = create
    return s


def test_llm_deploy_without_envs_still_asks():
    """Regression: the LLM called deploy with no envs and the user was never asked for DATABASE_URL."""
    async def go():
        c = MockLiara()
        s = _llm_session(c, [("deploy", {"source": VICTIM, "app": "shop-new", "create": True}), "تمام شد"])
        seen = {}
        async def drive():
            while True:
                ev = await s.queue.get()
                if ev["type"] == "approval":
                    seen["ap"] = ev
                    s.resolve(ev["id"], True, {"DATABASE_URL": "postgres://real/db"})
                if ev["type"] == "busy" and ev["value"] is False:
                    return
        await asyncio.gather(s.handle("پروژه رو دیپلوی کن"), drive())
        inputs = {i["key"]: i for i in seen["ap"]["inputs"]}
        assert "DATABASE_URL" in inputs and inputs["DATABASE_URL"]["required"], seen["ap"]["inputs"]
        assert "src/db.js" in inputs["DATABASE_URL"]["hint"]
        envs = {e["key"]: e["value"] for e in c.apps["shop-new"]["envs"]}
        assert envs["DATABASE_URL"] == "postgres://real/db"   # user's value, not the .env.example sample
    asyncio.run(go())


def test_deploy_prefills_from_project_dotenv_and_skips_existing():
    async def go():
        tmp = Path(tempfile.mkdtemp()) / "app"
        shutil.copytree(VICTIM, tmp)
        (tmp / ".env").write_text("DATABASE_URL=postgres://from-dotenv/db\n")
        c = MockLiara()
        s = Session(c)
        evs = await run(s, f"پروژه‌ی {tmp} را دیپلوی کن")
        ap = next(e for e in evs if e["type"] == "approval")
        inp = {i["key"]: i for i in ap["inputs"]}
        assert not inp["DATABASE_URL"]["required"] and ".env" in inp["DATABASE_URL"]["hint"]
        name = ap["args"]["app"]
        assert {e["key"]: e["value"] for e in c.apps[name]["envs"]}["DATABASE_URL"] == "postgres://from-dotenv/db"
        # deploying a project without .env onto an app that already has the variable -> not asked again
        evs2 = await run(Session(c), f"پروژه‌ی {VICTIM} را دیپلوی کن اسم shop-demo")
        ap2 = next(e for e in evs2 if e["type"] == "approval")
        assert "DATABASE_URL" not in {i["key"] for i in ap2["inputs"]}
        assert any("آنلاین" in e.get("text", "") for e in evs2 if e["type"] == "message")
        # secret values never land in the audit log
        assert "from-dotenv" not in open(os.environ["AUDIT_LOG_PATH"]).read()
    asyncio.run(go())
