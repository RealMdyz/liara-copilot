"""Risk policy + audit log (Human-in-the-Loop).

Every tool declares a risk level. The policy decides whether it runs
automatically or must wait for an explicit user approval.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum

from . import config


class Risk(str, Enum):
    READ = "read"          # 🟢 read-only: list, logs, metrics, status
    LOW = "low"            # 🟡 reversible, no downtime: upload source, add domain record
    HIGH = "high"          # 🔴 restart, env change (restarts app), deploy, rollback, plan change
    CRITICAL = "critical"  # ⛔ delete / irreversible — double confirmation


RISK_LABEL = {
    Risk.READ: "🟢 فقط خواندنی",
    Risk.LOW: "🟡 کم‌ریسک",
    Risk.HIGH: "🔴 حساس",
    Risk.CRITICAL: "⛔ غیرقابل‌بازگشت",
}


def needs_approval(risk: Risk) -> bool:
    if risk == Risk.READ:
        return False
    if risk == Risk.LOW:
        return config.APPROVAL_MODE == "strict"
    return True


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    args: dict
    risk: Risk
    title: str            # what will happen, in plain Persian
    impact: str           # downtime / cost / side effects
    rollback: str         # how to undo
    inputs: list = field(default_factory=list)   # values the user must type (e.g. env values)
    input_target: str = ""                       # arg name the inputs are merged into
    created: float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "id": self.id, "tool": self.tool, "args": self.args, "risk": self.risk.value,
            "risk_label": RISK_LABEL[self.risk], "title": self.title, "impact": self.impact,
            "rollback": self.rollback, "inputs": self.inputs,
        }


def audit(event: str, **data):
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **data}
    # never write secrets: mask env values
    if "args" in rec and isinstance(rec["args"], dict):
        args = dict(rec["args"])
        if "variables" in args and isinstance(args["variables"], dict):
            args["variables"] = {k: "***" for k in args["variables"]}
        rec["args"] = args
    try:
        with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    return rec


def read_audit(limit: int = 50) -> list[dict]:
    try:
        with open(config.AUDIT_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except FileNotFoundError:
        return []
