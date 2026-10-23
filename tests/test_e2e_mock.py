"""End-to-end scenarios against the Liara simulator (no network, no LLM).

Run:  MOCK_MODE=1 python -m pytest -q tests
"""
import asyncio
import os
from pathlib import Path

os.environ["MOCK_MODE"] = "1"
os.environ["LLM_API_KEY"] = ""
os.environ["AUDIT_LOG_PATH"] = "/tmp/copilot-test-audit.jsonl"
import tempfile as _tf  # noqa: E402
os.environ["DATA_DIR"] = _tf.mkdtemp(prefix="copilot-data-")

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
        assert "DATABASE_URL" in inputs and not inputs["DATABASE_URL"]["required"], seen["ap"]["inputs"]
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
        inp2 = {i["key"]: i for i in ap2["inputs"]}
        assert "روی لیارا تنظیم شده" in inp2["DATABASE_URL"]["hint"] and not inp2["DATABASE_URL"]["required"]
        assert {e["key"]: e["value"] for e in c.apps["shop-demo"]["envs"]}["DATABASE_URL"] == "postgres://db:5432/shop"
        assert any("آنلاین" in e.get("text", "") for e in evs2 if e["type"] == "message")
        # secret values never land in the audit log
        assert "from-dotenv" not in open(os.environ["AUDIT_LOG_PATH"]).read()
    asyncio.run(go())



def test_envs_optional_then_set_later_and_checked():
    async def go():
        c = MockLiara()
        s = Session(c)
        evs = await run(s, f"پروژه‌ی {VICTIM} را دیپلوی کن اسم env-later")   # leaves DATABASE_URL empty
        assert any("آنلاین" in e.get("text", "") for e in evs if e["type"] == "message")
        assert any(e["type"] == "step" and "خالی ماندند" in e["text"] for e in evs)
        evs = await run(s, "متغیرهای env-later را بررسی کن")
        chk = next(e for e in evs if e["type"] == "result" and e["tool"] == "check_envs")["data"]
        assert chk["missing"] == ["DATABASE_URL"]
        evs = await run(s, "متغیر DATABASE_URL=postgres://later/db را روی env-later بگذار")
        assert next(e for e in evs if e["type"] == "approval")["tool"] == "set_env"
        assert {e["key"]: e["value"] for e in c.apps["env-later"]["envs"]}["DATABASE_URL"] == "postgres://later/db"
        evs = await run(s, "متغیرهای env-later را بررسی کن")
        assert next(e for e in evs if e["type"] == "result" and e["tool"] == "check_envs")["data"]["missing"] == []
    asyncio.run(go())


def test_python_runtime_and_failed_deploy_diagnosis(tmp_path):
    """FastAPI app in app/main.py: must be started with uvicorn on 0.0.0.0, not `python3 main.py`."""
    async def go():
        proj = tmp_path / "fa"
        (proj / "app").mkdir(parents=True)
        (proj / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
        (proj / "app" / "__init__.py").write_text("")
        (proj / "requirements.txt").write_text("fastapi\n")
        c = MockLiara()
        s = Session(c)
        evs = await run(s, f"پروژه‌ی {proj} را دیپلوی کن اسم fa-app")
        ap = next(e for e in evs if e["type"] == "approval")
        assert "uvicorn app.main:app --host 0.0.0.0 --port 8000" in ap["impact"]
        body = c.apps["fa-app"]["last_release_body"]
        assert body["args"] == ["uvicorn app.main:app --host 0.0.0.0 --port 8000"] and body["port"] == 8000
    asyncio.run(go())


def test_diagnosis_finds_entry_and_localhost_problems():
    from copilot import diagnosis
    base = {"app": "x", "details": {"port": 8000, "envs": []}, "metrics": {}, "limitations": [],
            "probe": {"ok": False, "statuses": [502]}, "releases": [{"_id": "r2", "state": "UNHEALTHY"}],
            "source": "/p", "runtime": {"args": ["uvicorn app.main:app --host 0.0.0.0 --port 8000"]}}
    ev = {**base, "logs": [{"ts": 1, "line": "python3: can't open file '/usr/src/app/main.py': [Errno 2] No such file or directory"}]}
    top = diagnosis.analyze(ev)["hypotheses"][0]
    assert top["key"] == "entry" and top["fixes"][0]["args"]["start_command"].startswith("uvicorn")
    ev = {**base, "logs": [{"ts": 1, "line": "INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)"}]}
    top = diagnosis.analyze(ev)["hypotheses"][0]
    assert top["key"] == "host_binding"
    ev = {**base, "logs": [], "env_check": {"missing": ["OPENAI_API_KEY"], "empty": [], "suspicious": []}}
    assert diagnosis.analyze(ev)["hypotheses"][0]["key"] == "env_unset"


def test_conversation_history_persists_and_resumes():
    async def go():
        c = MockLiara()
        s = Session(c)
        await run(s, "وضعیت shop-demo")
        sid = s.id
        s2 = Session(c, sid=sid)                     # e.g. after a restart / switching conversations
        assert s2.title.startswith("وضعیت shop-demo")
        assert [e for e in s2.log if e["type"] == "user"][0]["text"] == "وضعیت shop-demo"
        assert any(e["type"] == "message" for e in s2.log)
    asyncio.run(go())


def test_api_conversations_and_voice_endpoint():
    from fastapi.testclient import TestClient
    from copilot import server
    cl = TestClient(server.app)
    sid = cl.post("/api/session").json()["sid"]
    s = server.SESSIONS[sid]
    asyncio.run(run(s, "پلن‌های لیارا"))
    lst = cl.get("/api/conversations").json()
    assert any(x["id"] == sid and x["messages"] >= 2 for x in lst)
    d = cl.post("/api/session", json={"resume": sid}).json()
    assert d["sid"] == sid and any(e["type"] == "user" for e in d["events"])
    # voice: without STT_API_KEY the server says so (the browser then uses its own speech recognition)
    r = cl.post("/api/transcribe", files={"file": ("v.webm", b"123", "audio/webm")})
    assert r.status_code == 501
    assert cl.delete(f"/api/conversations/{sid}").json()["ok"]
    assert not any(x["id"] == sid for x in cl.get("/api/conversations").json())
