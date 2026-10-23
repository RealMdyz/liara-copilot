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
        evs = await run(s, f"پروژه‌ی {VICTIM} را دیپلوی کن")
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
