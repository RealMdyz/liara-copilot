"""In-memory Liara simulator.

Used for automated tests and offline rehearsal when no LIARA_API_TOKEN is set.
It mirrors the LiaraClient interface and can inject realistic faults so the
SRE flow (diagnose -> fix -> verify) can be exercised end to end.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid

from .liara_client import LiaraError, Limitation

PLANS = {  # same shape as GET /v1/me -> plans.projects (price = Toman per hour)
    "micro-g2":    {"available": True, "region": "iran", "CPU": {"amount": 0.25}, "RAM": {"amount": 0.25}, "volume": 3,  "price": 140},
    "small-g2":    {"available": True, "region": "iran", "CPU": {"amount": 0.5},  "RAM": {"amount": 0.5},  "volume": 5,  "price": 280},
    "medium-g2":   {"available": True, "region": "iran", "CPU": {"amount": 1},    "RAM": {"amount": 1},    "volume": 10, "price": 560},
    "standard-g2": {"available": True, "region": "iran", "CPU": {"amount": 2},    "RAM": {"amount": 2},    "volume": 20, "price": 1100},
    "pro-g2":      {"available": True, "region": "iran", "CPU": {"amount": 4},    "RAM": {"amount": 4},    "volume": 40, "price": 2200},
    "small":       {"available": False, "region": "iran", "CPU": {"amount": 0.5}, "RAM": {"amount": 0.5}, "volume": 5, "price": 250},
}
BUNDLES = {pid: {"basic": {"available": True, "displayPrice": 0}, "standard": {"available": True, "displayPrice": 90_000}}
           for pid in PLANS}
VM_PLANS = {
    "standard-base-g2": {"available": True, "CPU": {"amount": 1}, "RAM": {"amount": 1}, "volume": 25, "monthlyPrice": 390_000},
    "standard-plus-g2": {"available": True, "CPU": {"amount": 2}, "RAM": {"amount": 2}, "volume": 40, "monthlyPrice": 690_000},
    "pro-g2":           {"available": True, "CPU": {"amount": 2}, "RAM": {"amount": 4}, "volume": 60, "monthlyPrice": 1_190_000},
    "pro-plus-g2":      {"available": True, "CPU": {"amount": 4}, "RAM": {"amount": 8}, "volume": 80, "monthlyPrice": 2_190_000},
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MockLiara:
    is_mock = True

    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.domains: list[dict] = []
        self.sources: dict[str, str] = {}
        self.releases: dict[str, dict] = {}
        self.vms: dict[str, dict] = {}
        self._seed()

    # ---------------------------------------------------------------- seed
    def _seed(self):
        self._make_app("shop-demo", "node", "small-g2", port=3000,
                       envs={"NODE_ENV": "production", "DATABASE_URL": "postgres://db:5432/shop"})

    def _make_app(self, name, platform, plan, port=3000, envs=None):
        self.apps[name] = {
            "name": name, "project_id": name, "_id": uuid.uuid4().hex[:24], "type": platform,
            "planID": plan, "port": port, "scale": 1, "status": "RUNNING",
            "envs": [{"key": k, "value": v} for k, v in (envs or {}).items()],
            "fault": None, "mem": 0.35, "cpu": 0.12, "createdAt": _now(),
            "release_ids": [],
        }
        rid = self._new_release(name, "READY")
        return rid

    def _new_release(self, app, state, tag=None):
        rid = uuid.uuid4().hex[:24]
        self.releases[rid] = {"_id": rid, "project": app, "state": state, "createdAt": _now(),
                              "tag": tag or f"v{len(self.apps[app]['release_ids']) + 1}", "queue": 0}
        self.apps[app]["release_ids"].append(rid)
        return rid

    def inject_fault(self, app: str, fault: str | None):
        a = self._app(app)
        a["fault"] = fault
        if fault == "missing_env":
            a["envs"] = [e for e in a["envs"] if e["key"] != "DATABASE_URL"]
        if fault == "memory_leak":
            a["mem"] = 0.96
        if fault == "cpu":
            a["cpu"] = 0.98
        if fault == "bad_release":
            self._new_release(app, "UNHEALTHY")

    def _app(self, app):
        if app not in self.apps:
            raise LiaraError(404, "Project not found", f"/v1/projects/{app}")
        return self.apps[app]

    # --------------------------------------------------------------- account
    async def me(self):
        return {"user": {"email": "demo@example.com", "balance": 2_500_000},
                "plans": {"projects": PLANS, "projectBundlePlans": BUNDLES}}

    async def list_plans(self):
        return PLANS

    async def creatable_plans(self):
        return {k: v for k, v in PLANS.items() if "g2" in k and v["available"]}

    async def bundle_plans(self, plan_id):
        return BUNDLES.get(plan_id, {})

    async def change_plan(self, app, plan_id):
        a = self._app(app)
        if plan_id not in PLANS:
            raise LiaraError(400, "invalid planID", "resize")
        a["planID"] = plan_id
        if a["fault"] in ("memory_leak", "memory_leak_recovering", "cpu"):
            a["mem"], a["cpu"], a["fault"] = 0.4, 0.3, None
        return {"message": "plan changed"}

    async def events(self, app, minutes=180):
        a = self._app(app)
        if a["fault"] == "memory_leak":
            return ["OOMKilled: container exceeded memory limit", "Container restarted (reason: OOMKilled)"]
        return ["Deployment succeeded"]

    # ------------------------------------------------------------------ IaaS
    async def vm_plans(self):
        return VM_PLANS

    async def vm_os_list(self):
        return {"ubuntu": ["24.04", "22.04"], "debian": ["12.9"]}

    async def vm_list(self):
        return [{k: v for k, v in vm.items() if not k.startswith("_t")} for vm in self.vms.values()]

    async def vm_get(self, vm_id):
        for vm in self.vms.values():
            if vm["_id"] == vm_id:
                if vm["state"] != "CREATED" and time.time() - vm["_t"] > 1.0:
                    vm["state"], vm["power"] = "CREATED", "ON"
                return {k: v for k, v in vm.items() if not k.startswith("_t")}
        raise LiaraError(404, "VM not found", "vm")

    async def vm_find(self, name_or_id):
        for vm in self.vms.values():
            if name_or_id in (vm["_id"], vm["name"]):
                return await self.vm_get(vm["_id"])
        raise LiaraError(404, f"سرور «{name_or_id}» پیدا نشد", "vm")

    async def vm_create(self, name, os, plan, ssh_keys=None):
        if name in self.vms:
            raise LiaraError(409, "VM exists", "vm")
        if plan not in VM_PLANS:
            raise LiaraError(400, "invalid plan", "vm")
        self.vms[name] = {"_id": uuid.uuid4().hex[:24], "name": name, "OS": os, "plan": plan, "state": "CREATING",
                          "power": "OFF", "createdAt": _now(), "_t": time.time(),
                          "IPs": [{"address": f"185.{random.randint(1,250)}.{random.randint(1,250)}.{random.randint(1,250)}", "version": "v4"}],
                          "config": {"hostname": name, "rootPassword": uuid.uuid4().hex[:14], "SSHKeys": ssh_keys or []},
                          "planDetails": VM_PLANS[plan]}
        return {"taskID": uuid.uuid4().hex[:12]}

    async def vm_power(self, vm_id, action):
        vm = await self.vm_get(vm_id)
        self.vms[vm["name"]]["power"] = "ON" if action in ("start", "reboot") else "OFF"
        return {"taskID": uuid.uuid4().hex[:12]}

    async def vm_resize(self, vm_id, plan):
        vm = await self.vm_get(vm_id)
        self.vms[vm["name"]].update(plan=plan, planDetails=VM_PLANS[plan])
        return {"taskID": uuid.uuid4().hex[:12]}

    async def vm_reset_password(self, vm_id):
        vm = await self.vm_get(vm_id)
        if vm["power"] != "OFF":
            raise LiaraError(409, "VM is not powered off", "reset-password")
        pw = uuid.uuid4().hex[:14]
        self.vms[vm["name"]]["config"]["rootPassword"] = pw
        return {"password": pw}

    async def vm_operations(self, vm_id):
        return [{"name": "create", "state": "SUCCESS", "createdAt": _now()}]

    async def vm_delete(self, vm_id):
        vm = await self.vm_get(vm_id)
        del self.vms[vm["name"]]
        return {}

    async def mock_ssh(self, vm, cmds, ctx):
        await ctx.step(f"اتصال SSH به ‎root@{vm['IPs'][0]['address']}‎ (شبیه‌سازی)")
        results = []
        for c in cmds:
            if c.startswith("export "):
                continue
            await asyncio.sleep(0.05)
            out = "ok"
            if "docker --version" in c:
                out = "Docker version 26.1.3, build 26.1.3"
            elif "free -h" in c:
                out = "Mem: 1.9Gi used 412Mi free 1.2Gi"
            results.append({"cmd": c, "exit": 0, "tail": out})
            await ctx.step(f"‎$ {c[:70]}‎", "done", detail=out)
        return {"ok": True, "ip": vm["IPs"][0]["address"], "results": results}

    async def close(self):
        pass

    # ------------------------------------------------------------------ apps
    async def list_apps(self):
        return [{k: v for k, v in a.items() if k not in ("fault", "release_ids")} for a in self.apps.values()]

    async def get_app(self, app):
        a = self._app(app)
        return {k: v for k, v in a.items() if k not in ("fault", "release_ids")}

    async def create_app(self, name, platform, plan_id, bundle_plan_id="standard", network=None):
        if name in self.apps:
            raise LiaraError(409, "Project already exists", "/v1/projects/")
        if plan_id not in PLANS or "g2" not in plan_id or not PLANS[plan_id]["available"]:
            raise LiaraError(400, "Invalid planID: only g2 plans are available for new apps", "/v1/projects")
        self.apps[name] = {
            "name": name, "project_id": name, "_id": uuid.uuid4().hex[:24], "type": platform,
            "planID": plan_id, "port": 3000, "scale": 1, "status": "CREATED", "envs": [],
            "fault": None, "mem": 0.1, "cpu": 0.05, "createdAt": _now(), "release_ids": [],
        }
        return {"created": name, "planID": plan_id, "bundlePlanID": "standard", "network": "net1"}

    async def delete_app(self, app):
        self._app(app)
        del self.apps[app]
        return {}

    async def restart_app(self, app):
        a = self._app(app)
        if a["fault"] == "memory_leak":
            a["mem"] = 0.30
            a["fault"] = "memory_leak_recovering"
        a["status"] = "RUNNING"
        await asyncio.sleep(0.2)
        return {"message": "restarted"}

    async def scale_app(self, app, scale):
        a = self._app(app)
        a["scale"] = scale
        a["status"] = "RUNNING" if scale else "STOPPED"
        return {}

    async def update_port(self, app, port):
        a = self._app(app)
        a["port"] = port
        if a["fault"] == "port" and port == 3000:
            a["fault"] = None
        return {}

    async def list_networks(self):
        return [{"_id": "net1", "name": "default"}]

    # ------------------------------------------------------------------ envs
    async def get_envs(self, app):
        return [dict(e) for e in self._app(app)["envs"]]

    async def set_envs(self, app, new_vars, remove=None):
        a = self._app(app)
        cur = {e["key"]: e["value"] for e in a["envs"]}
        cur.update(new_vars)
        for k in remove or []:
            cur.pop(k, None)
        a["envs"] = [{"key": k, "value": v} for k, v in cur.items()]
        if a["fault"] == "missing_env" and "DATABASE_URL" in cur:
            a["fault"] = None
        return {"message": "envs updated, restarting"}

    # ---------------------------------------------------------------- deploy
    async def upload_source(self, app, archive_path):
        self._app(app)
        sid = uuid.uuid4().hex[:24]
        self.sources[sid] = archive_path
        return sid

    async def create_release(self, app, source_id, platform, port, envs=None, message="", platform_config=None, args=None):
        a = self._app(app)
        a["port"] = port or a["port"]
        a["last_release_body"] = {"port": port, "args": args, "platformConfig": platform_config}
        rid = self._new_release(app, "BUILDING")
        self.releases[rid]["_progress"] = 0
        return {"releaseID": rid}

    async def get_release(self, release_id):
        r = self.releases[release_id]
        if r["state"] in ("BUILDING", "PUSHING", "DEPLOYING"):
            r["_progress"] = r.get("_progress", 0) + 1
            r["state"] = ["BUILDING", "BUILDING", "PUSHING", "DEPLOYING", "READY"][min(r["_progress"], 4)]
            if r["state"] == "READY":
                a = self.apps[r["project"]]
                a["status"] = "RUNNING"
        return {k: v for k, v in r.items() if k != "_progress"}

    async def build_logs(self, release_id, since=None):
        r = self.releases[release_id]
        lines = {
            "BUILDING": ["Step 1/6 : FROM node:20-alpine", "Step 3/6 : RUN npm ci --omit=dev",
                         "added 57 packages in 3s"],
            "PUSHING": ["Successfully tagged app:latest", "Pushing... 100%"],
        }.get(r["state"], [])
        return {"release": {"state": r["state"]},
                "buildOutput": [{"stream": "STDOUT", "line": l, "createdAt": _now()} for l in lines]}

    async def list_releases(self, app, count=10):
        ids = self._app(app)["release_ids"][-count:]
        return [{k: v for k, v in self.releases[i].items() if k != "_progress"} for i in reversed(ids)]

    async def rollback(self, app, release_id):
        a = self._app(app)
        if self.releases[release_id]["state"] != "READY":
            raise LiaraError(400, "Can only roll back to a READY release", "rollback")
        self._new_release(app, "READY", tag=self.releases[release_id]["tag"] + "-rollback")
        if a["fault"] == "bad_release":
            a["fault"] = None
        return {"message": "rolled back"}

    # --------------------------------------------------------- observability
    async def logs(self, app, minutes=30, limit=300, release_id=None):
        a = self._app(app)
        base = int(time.time()) - minutes * 60
        healthy = [
            "Server listening on port 3000",
            "GET / 200 12ms",
            "GET /api/products 200 34ms",
            "GET /health 200 2ms",
        ]
        bad = {
            "missing_env": [
                "GET /api/products 500 8ms",
                "Error: DATABASE_URL is not set. Cannot connect to database",
                "    at getDb (/app/src/db.js:7:11)",
                "    at /app/server.js:41:20",
                "GET /api/products 500 6ms",
            ],
            "memory_leak": [
                "GET /api/products 200 2380ms",
                "<--- Last few GCs --->",
                "[1:0x5a1] 98234 ms: Mark-Compact 236.1 (251.4) -> 233.9 (250.1) MB, 812 ms",
                "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
                "    at cacheResult (/app/server.js:58:9)",
                "Container killed: OOMKilled (memory limit reached)",
                "Server listening on port 3000",
                "GET /api/products 200 1950ms",
            ],
            "cpu": [
                "GET /api/report 200 8721ms",
                "GET / 200 3120ms",
                "GET /api/report 504 30000ms",
            ],
            "bad_release": [
                "Error: Cannot find module 'express-rate-limit'",
                "Require stack:",
                "- /app/server.js",
                "Health check failed: no response on port 3000",
            ],
            "port": [
                "Server listening on port 8080",
                "Health check failed: connection refused on port 3000",
            ],
        }
        lines = healthy + (bad.get(a["fault"], []) * 3 if a["fault"] else [])
        out = [{"ts": base + i * 7, "line": l, "releaseId": a["release_ids"][-1]} for i, l in enumerate(lines)]
        return out[-limit:]

    async def metrics(self, app):
        a = self._app(app)
        plan = PLANS[a["planID"]]
        return {
            "period": "1h",
            "metrics": {
                "cpu": round(a["cpu"] * 100, 1), "memory": round(a["mem"] * 100, 1),
                "memoryLimitMB": int(plan["RAM"]["amount"] * 1024), "cpuLimit": plan["CPU"]["amount"],
                "restarts1h": 3 if a["fault"] == "memory_leak" else 0,
            },
        }

    async def mock_probe(self, app):
        a = self.apps.get(app)
        if not a or a["scale"] == 0:
            return {"status": 503, "latency_ms": 40, "error": "service unavailable"}
        f = a["fault"]
        if f == "missing_env":
            return {"status": 500, "latency_ms": 9}
        if f == "memory_leak":
            return {"status": 200, "latency_ms": random.randint(1900, 2600)}
        if f == "cpu":
            return {"status": 200, "latency_ms": random.randint(3000, 8000)}
        if f in ("bad_release", "port"):
            return {"status": 502, "latency_ms": 30}
        return {"status": 200, "latency_ms": random.randint(25, 80)}

    # --------------------------------------------------------------- domains
    async def list_domains(self, app):
        return [d for d in self.domains if d["project"] == app]

    async def add_domain(self, app, domain):
        d = {"_id": uuid.uuid4().hex[:24], "name": domain, "project": app, "status": "PENDING"}
        self.domains.append(d)
        return {"domain": d}

    async def check_domain(self, domain_id):
        return {"status": "PENDING", "message": "DNS records not propagated yet"}
