"""Web server: chat UI + streaming events + approval endpoint."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import config, diagnosis
from .agent import CONV_DIR, Session
from .liara_client import LiaraClient
from .mock_liara import MockLiara
from .policy import read_audit

app = FastAPI(title="Liara Copilot")
CLIENT = MockLiara() if config.MOCK_MODE else LiaraClient()
SESSIONS: dict[str, Session] = {}
WEB = Path(__file__).parent / "web"


class ChatIn(BaseModel):
    sid: str
    text: str


class ApproveIn(BaseModel):
    sid: str
    id: str
    approved: bool
    values: dict | None = None


class FaultIn(BaseModel):
    app: str
    fault: str | None = None


def _session(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Voice input: audio from the mic button -> text (OpenAI-compatible /audio/transcriptions)."""
    if not config.STT_API_KEY:
        raise HTTPException(501, "تبدیل گفتار به متن روی سرور تنظیم نشده (STT_API_KEY)")
    from openai import AsyncOpenAI
    data = await file.read()
    if len(data) > 25_000_000:
        raise HTTPException(413, "فایل صوتی خیلی بزرگ است")
    cli = AsyncOpenAI(api_key=config.STT_API_KEY, base_url=config.STT_BASE_URL or None)
    try:
        r = await cli.audio.transcriptions.create(model=config.STT_MODEL, file=(file.filename or "voice.webm", data),
                                                  language="fa")
    except Exception as e:  # noqa
        raise HTTPException(502, f"سرویس تبدیل گفتار خطا داد: {type(e).__name__}")
    return {"text": getattr(r, "text", "") or ""}


@app.get("/api/info")
async def info():
    return {"mock": config.MOCK_MODE, "llm": bool(config.LLM_API_KEY), "model": config.LLM_MODEL if config.LLM_API_KEY else None,
            "approval_mode": config.APPROVAL_MODE, "stt": bool(config.STT_API_KEY)}


class SessionIn(BaseModel):
    resume: str | None = None


@app.post("/api/session")
async def new_session(body: SessionIn | None = None):
    rid = (body.resume if body else None) or None
    if rid:
        if not re.fullmatch(r"[a-f0-9]{6,32}", rid):
            raise HTTPException(400, "bad id")
        s = SESSIONS.get(rid) or Session(CLIENT, sid=rid)
        SESSIONS[s.id] = s
        return {"sid": s.id, "title": s.title, "events": s.log, "pending": list(s.pending.keys()), "busy": s.busy}
    s = Session(CLIENT)
    SESSIONS[s.id] = s
    return {"sid": s.id, "events": [], "pending": []}


@app.get("/api/conversations")
async def conversations():
    out = []
    if os.path.isdir(CONV_DIR):
        for fn in os.listdir(CONV_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(CONV_DIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"id": d["id"], "title": d.get("title"), "updated": d.get("updated"),
                            "messages": sum(1 for e in d.get("events", []) if e.get("type") in ("user", "message"))})
            except (OSError, ValueError, KeyError):
                continue
    return sorted(out, key=lambda x: -(x["updated"] or 0))[:200]


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str):
    if not re.fullmatch(r"[a-f0-9]{6,32}", cid):
        raise HTTPException(400, "bad id")
    SESSIONS.pop(cid, None)
    try:
        os.remove(os.path.join(CONV_DIR, f"{cid}.json"))
    except OSError:
        pass
    return {"ok": True}


@app.get("/api/events/{sid}")
async def events(sid: str):
    s = _session(sid)

    async def gen():
        yield "retry: 2000\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(s.queue.get(), timeout=15)
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat")
async def chat(body: ChatIn):
    s = _session(body.sid)
    asyncio.create_task(s.handle(body.text))
    return {"ok": True}


@app.post("/api/approve")
async def approve(body: ApproveIn):
    s = _session(body.sid)
    if not s.resolve(body.id, body.approved, body.values):
        raise HTTPException(404, "approval not found or already answered")
    return {"ok": True}


@app.get("/api/dashboard")
async def dashboard():
    try:
        apps = await CLIENT.list_apps()
    except Exception as e:  # noqa
        return {"error": str(e), "apps": []}

    async def one(a):
        name = a.get("project_id") or a.get("name")
        pr = await diagnosis.probe(CLIENT, name, n=1)
        m = {}
        try:
            m = (await CLIENT.metrics(name)).get("metrics", {})
        except Exception:
            pass
        return {"name": name, "plan": a.get("planID"), "status": a.get("status"), "platform": a.get("type"),
                "http": pr.get("statuses", [None])[0], "latency_ms": pr.get("median_ms"),
                "cpu": m.get("cpu"), "mem": m.get("memory")}

    vms = []
    try:
        for v in await CLIENT.vm_list():
            ip = ""
            try:
                full = await CLIENT.vm_get(v["_id"])
                ip = ", ".join(i.get("address", "") for i in full.get("IPs", []))[:40]
                v = full
            except Exception:
                pass
            vms.append({"name": v.get("name"), "state": v.get("state"), "power": v.get("power"),
                        "plan": v.get("plan"), "os": v.get("OS"), "ip": ip})
    except Exception:
        pass
    return {"apps": await asyncio.gather(*[one(a) for a in apps]), "vms": vms, "mock": config.MOCK_MODE}


@app.get("/api/audit")
async def audit():
    return read_audit(50)


@app.post("/api/mock/fault")
async def mock_fault(body: FaultIn):
    if not config.MOCK_MODE:
        raise HTTPException(400, "only in mock mode")
    CLIENT.inject_fault(body.app, body.fault)
    return {"ok": True}


def main():
    import uvicorn
    print(f"\n  Liara Copilot  →  http://localhost:{config.PORT}   "
          f"(mode: {'MOCK' if config.MOCK_MODE else 'LIVE Liara API'}, brain: {'LLM ' + config.LLM_MODEL if config.LLM_API_KEY else 'rule-based'})\n")
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
