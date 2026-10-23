"""MCP server: exposes the same tools to Claude Desktop / Cursor / any MCP client.

Read-only tools run directly. Risky tools follow a two-step confirmation:
the first call returns a confirmation card + `confirm_token`; the tool only
executes when called again with that token (after the human says yes in chat).
"""
from __future__ import annotations

import json
import secrets
import time

from mcp.server.fastmcp import FastMCP

from . import config
from .liara_client import LiaraClient, LiaraError, Limitation
from .mock_liara import MockLiara
from .policy import Risk, audit, needs_approval
from .tools import REGISTRY, Ctx

mcp = FastMCP("liara-copilot")
CLIENT = MockLiara() if config.MOCK_MODE else LiaraClient()
STATE: dict = {}
PENDING: dict[str, tuple[str, str, float]] = {}  # token -> (tool, args_json, expires)


async def _noop(ev):  # progress events are not streamed over MCP
    pass


async def run_tool(name: str, args: dict, confirm_token: str | None = None) -> str:
    t = REGISTRY[name]
    if needs_approval(t.risk):
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        tok = PENDING.get(confirm_token or "")
        if not tok or tok[0] != name or tok[1] != key or tok[2] < time.time():
            new = secrets.token_hex(4)
            PENDING[new] = (name, key, time.time() + 600)
            title, impact, rollback = t.approval(args) if t.approval else (name, "", "")
            audit("approval_requested", tool=name, args=args, via="mcp")
            return json.dumps({
                "requires_confirmation": True, "risk": t.risk.value, "action": title, "impact": impact,
                "undo": rollback, "confirm_token": new,
                "instruction": "Show this to the user and ask for an explicit yes. Only if they agree, "
                               "call the same tool again with identical arguments plus confirm_token.",
            }, ensure_ascii=False)
        PENDING.pop(confirm_token, None)
        audit("approval_granted", tool=name, via="mcp")
    try:
        res = await t.fn(Ctx(CLIENT, _noop, STATE), **args)
    except Limitation as e:
        res = {"limitation": str(e)}
    except LiaraError as e:
        res = {"error": e.human(), "status": e.status}
    audit("tool_executed", tool=name, args=args, via="mcp")
    return json.dumps(res, ensure_ascii=False, default=str)[:20000]


# ---- explicit wrappers (FastMCP needs typed signatures) -------------------
@mcp.tool()
async def list_apps() -> str:
    """List all Liara apps with status and plan."""
    return await run_tool("list_apps", {})


@mcp.tool()
async def app_status(app: str) -> str:
    """Live status of one app: running state, plan, port, env keys, releases, HTTP check."""
    return await run_tool("app_status", {"app": app})


@mcp.tool()
async def get_logs(app: str, minutes: int = 30, query: str = "") -> str:
    """Recent logs of an app, optional text filter."""
    return await run_tool("get_logs", {"app": app, "minutes": minutes, **({"query": query} if query else {})})


@mcp.tool()
async def diagnose(app: str, symptom: str = "") -> str:
    """SRE diagnosis: collects logs, status, releases, metrics and an HTTP probe, then ranks root causes with evidence and fixes."""
    return await run_tool("diagnose", {"app": app, "symptom": symptom})


@mcp.tool()
async def verify_fix(app: str) -> str:
    """Re-check the app after a fix and compare with the last diagnosis."""
    return await run_tool("verify_fix", {"app": app})


@mcp.tool()
async def health_check_all() -> str:
    """Health check of every app in the account."""
    return await run_tool("health_check_all", {})


@mcp.tool()
async def analyze_project(source: str) -> str:
    """Analyze a local folder or git URL: platform, port, required env vars, recommended Liara plan."""
    return await run_tool("analyze_project", {"source": source})


@mcp.tool()
async def list_plans() -> str:
    """Available Liara plans (RAM / CPU / price)."""
    return await run_tool("list_plans", {})


@mcp.tool()
async def domain_dns_guide(app: str, domain: str) -> str:
    """Step-by-step DNS guide for the domain registrar (e.g. nic.ir)."""
    return await run_tool("domain_dns_guide", {"app": app, "domain": domain})


@mcp.tool()
async def deploy(source: str, app: str, create: bool = False, plan_id: str = "", platform: str = "",
                 port: int = 0, envs: dict | None = None, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Package, upload and release a project (optionally creating the app)."""
    args = {"source": source, "app": app, "create": create}
    args |= {k: v for k, v in {"plan_id": plan_id, "platform": platform, "port": port or None, "envs": envs}.items() if v}
    return await run_tool("deploy", args, confirm_token or None)


@mcp.tool()
async def set_env(app: str, variables: dict, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Set environment variables (restarts the app)."""
    return await run_tool("set_env", {"app": app, "variables": variables}, confirm_token or None)


@mcp.tool()
async def restart_app(app: str, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Restart an app (short downtime)."""
    return await run_tool("restart_app", {"app": app}, confirm_token or None)


@mcp.tool()
async def start_app(app: str, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Start a stopped app."""
    return await run_tool("start_app", {"app": app}, confirm_token or None)


@mcp.tool()
async def update_port(app: str, port: int, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Change the port Liara routes traffic to."""
    return await run_tool("update_port", {"app": app, "port": port}, confirm_token or None)


@mcp.tool()
async def rollback(app: str, release_id: str, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Roll back to a previous healthy release."""
    return await run_tool("rollback", {"app": app, "release_id": release_id}, confirm_token or None)


@mcp.tool()
async def add_domain(app: str, domain: str, confirm_token: str = "") -> str:
    """Attach a domain to an app."""
    return await run_tool("add_domain", {"app": app, "domain": domain}, confirm_token or None)


@mcp.tool()
async def change_plan(app: str, plan_id: str, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Change an app's plan (CPU/RAM) — only g2 plans from list_plans."""
    return await run_tool("change_plan", {"app": app, "plan_id": plan_id}, confirm_token or None)


@mcp.tool()
async def list_vms() -> str:
    """List Liara virtual machines (IaaS) with IP, OS, plan and power state."""
    return await run_tool("list_vms", {})


@mcp.tool()
async def vm_options() -> str:
    """Available VM plans (CPU/RAM/disk/monthly price), operating systems and setup recipes."""
    return await run_tool("vm_options", {})


@mcp.tool()
async def create_vm(name: str, plan: str, os: str = "ubuntu-24.04", confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Create a Liara virtual machine and wait until it is ready."""
    return await run_tool("create_vm", {"name": name, "plan": plan, "os": os}, confirm_token or None)


@mcp.tool()
async def vm_power(vm: str, action: str, confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] start | stop | reboot | shutdown a virtual machine."""
    return await run_tool("vm_power", {"vm": vm, "action": action}, confirm_token or None)


@mcp.tool()
async def vm_setup(vm: str, recipes: list[str], confirm_token: str = "") -> str:
    """[REQUIRES CONFIRMATION] Configure software on a VM over SSH. recipes: update, security, docker, nginx, node, python, postgres, swap."""
    return await run_tool("vm_setup", {"vm": vm, "recipes": recipes}, confirm_token or None)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
