"""Async client for the Liara APIs (PaaS + IaaS).

Endpoints follow the official documentation (developers.liara.ir, OpenAPI specs
paas.yaml / Iaas.yaml) and the official liara-cli source. Every Liara error is
surfaced with its real message so the user (and the LLM) see *why* an operation
failed instead of a guess.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from . import config

IAAS_BASE = "https://iaas-api.liara.ir"  # developers.liara.ir/pages/iaas.md


class LiaraError(Exception):
    def __init__(self, status: int, message: str, path: str = "", code: str = ""):
        self.status, self.message, self.path, self.code = status, message, path, code
        super().__init__(f"[{status}] {path}: {message}")

    def human(self) -> str:
        hints = {
            401: "توکن API نامعتبر یا منقضی است. از console.liara.ir/API توکن جدید بگیرید.",
            402: "موجودی حساب لیارا کافی نیست. لطفاً ابتدا حساب را شارژ کنید (این کار فقط از دست خودتان برمی‌آید).",
            403: "این توکن اجازه‌ی این عملیات را ندارد.",
            404: "منبع موردنظر پیدا نشد (نام را بررسی کنید).",
            409: "منبعی با این نام از قبل وجود دارد (نام‌ها در کل لیارا یکتا هستند).",
            412: "پیش‌شرط عملیات برقرار نیست.",
            429: "تعداد درخواست‌ها زیاد است؛ چند ثانیه بعد دوباره تلاش کنید.",
        }
        base = hints.get(self.status, "")
        detail = self.message if self.message and self.message not in base else ""
        if base and detail:
            return f"{base} (پیام لیارا: {detail})"
        return base or detail or "خطای ناشناخته از API لیارا"


class Limitation(Exception):
    """Raised when an operation is not available through Liara's public API."""


class LiaraClient:
    is_mock = False

    def __init__(self, token: str | None = None, base: str | None = None, team_id: str | None = None):
        self.token = token or config.LIARA_API_TOKEN
        self.base = (base or config.LIARA_API_BASE).rstrip("/")
        self.team_id = team_id if team_id is not None else config.LIARA_TEAM_ID
        self._http = httpx.AsyncClient(
            base_url=self.base,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(30.0, read=120.0),
        )
        self._me_cache: tuple[float, dict] | None = None

    async def close(self):
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def _req(self, method: str, path: str, **kw) -> Any:
        params = dict(kw.pop("params", None) or {})
        if self.team_id:
            params["teamID"] = self.team_id
        try:
            r = await self._http.request(method, path, params=params, **kw)
        except httpx.HTTPError as e:
            raise LiaraError(0, f"ارتباط با API لیارا برقرار نشد: {type(e).__name__}", path)
        if r.status_code >= 400:
            msg, code = r.text[:300], ""
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or msg
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                code = str(data.get("code") or body.get("code") or "")
                if code and code not in str(msg):
                    msg = f"{msg} ({code})"
            except Exception:
                pass
            raise LiaraError(r.status_code, str(msg), path, code)
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    # ---------------------------------------------------------------- account
    async def me(self, fresh: bool = False) -> dict:
        if not fresh and self._me_cache and time.time() - self._me_cache[0] < 120:
            return self._me_cache[1]
        data = await self._req("GET", "/v1/me")
        self._me_cache = (time.time(), data)
        return data

    async def list_plans(self) -> dict:
        """All app plans (GET /v1/me -> plans.projects)."""
        return ((await self.me()).get("plans") or {}).get("projects") or {}

    async def creatable_plans(self) -> dict:
        """Only plans new apps can be created on — same filter as `liara app create`:
        generation-2 plans (id contains 'g2') that are available in the Iran region, plus 'free'."""
        plans = await self.list_plans()
        out = {}
        for pid, p in plans.items():
            if not isinstance(p, dict) or not p.get("available", True):
                continue
            if pid == "free" or ("g2" in pid and p.get("region", "iran") == "iran"):
                out[pid] = p
        return out or plans

    async def bundle_plans(self, plan_id: str) -> dict:
        """Feature-bundle plans valid for a given app plan (plans.projectBundlePlans[planID])."""
        bp = ((await self.me()).get("plans") or {}).get("projectBundlePlans") or {}
        return {k: v for k, v in (bp.get(plan_id) or {}).items() if isinstance(v, dict) and v.get("available", True)}

    # ------------------------------------------------------------------- apps
    async def list_apps(self) -> list[dict]:
        data = await self._req("GET", "/v1/projects")
        return data.get("projects", [])

    async def get_app(self, app: str) -> dict:
        data = await self._req("GET", f"/v1/projects/{app}")
        return data.get("project", data)

    async def list_networks(self) -> list[dict]:
        data = await self._req("GET", "/v1/networks")
        return data.get("networks", data if isinstance(data, list) else [])

    async def create_network(self, name: str) -> dict:
        return await self._req("POST", "/v1/networks", json={"networkName": name})

    async def create_app(self, name: str, platform: str, plan_id: str, bundle_plan_id: str | None = None,
                         network: str | None = None) -> dict:
        """POST /v1/projects. Picks a valid feature-bundle and private network automatically
        (both are required for g2 plans, exactly like `liara app create`)."""
        if bundle_plan_id is None and plan_id != "free":
            bundles = await self.bundle_plans(plan_id)
            if bundles:
                order = ["standard", "basic", "pro"]
                bundle_plan_id = sorted(bundles, key=lambda k: (order.index(k) if k in order else 9))[0]
        if plan_id == "free":
            bundle_plan_id = "free"
        if network is None:
            nets = await self.list_networks()
            if not nets:
                await self.create_network("copilot-net")
                nets = await self.list_networks()
            if nets:
                network = nets[0].get("_id") or nets[0].get("id")
        body: dict = {"name": name, "planID": plan_id, "platform": platform}
        if bundle_plan_id:
            body["bundlePlanID"] = bundle_plan_id
        if network:
            body["network"] = network
        res = await self._req("POST", "/v1/projects", json=body)
        return {"created": name, "planID": plan_id, "bundlePlanID": bundle_plan_id, "network": network, "api": res}

    async def change_plan(self, app: str, plan_id: str) -> dict:
        """POST /v1/projects/{name}/resize  {planID}  (developers.liara.ir: Change plan)."""
        return await self._req("POST", f"/v1/projects/{app}/resize", json={"planID": plan_id})

    async def delete_app(self, app: str) -> dict:
        return await self._req("DELETE", f"/v1/projects/{app}")

    async def restart_app(self, app: str) -> dict:
        return await self._req("POST", f"/v1/projects/{app}/actions/restart")

    async def scale_app(self, app: str, scale: int) -> dict:
        return await self._req("POST", f"/v1/projects/{app}/actions/scale", json={"scale": scale})

    async def update_port(self, app: str, port: int) -> dict:
        return await self._req("POST", "/v1/projects/update-port", json={"project": app, "port": port})

    # ------------------------------------------------------------------- envs
    async def get_envs(self, app: str) -> list[dict]:
        project = await self.get_app(app)
        return [{"key": e.get("key"), "value": e.get("value")} for e in project.get("envs", [])]

    async def set_envs(self, app: str, new_vars: dict[str, str], remove: list[str] | None = None) -> dict:
        """Liara replaces the whole env list (same as `liara env set`), so we merge first."""
        current = {e["key"]: e["value"] for e in await self.get_envs(app)}
        current.update(new_vars)
        for k in remove or []:
            current.pop(k, None)
        variables = [{"key": k, "value": v} for k, v in current.items()]
        return await self._req("POST", "/v1/projects/update-envs", json={"project": app, "variables": variables})

    # ---------------------------------------------------------------- deploy
    async def upload_source(self, app: str, archive_path: str) -> str:
        with open(archive_path, "rb") as f:
            data = await self._req("POST", f"/v2/projects/{app}/sources",
                                   files={"file": ("source.tar.gz", f, "application/gzip")})
        return data.get("sourceID") or data.get("_id") or data.get("id")

    async def create_release(self, app: str, source_id: str, platform: str, port: int | None,
                             envs: dict | None = None, message: str = "Deployed by Liara Copilot",
                             platform_config: dict | None = None, args: list | None = None) -> dict:
        body = {"build": {"cache": True}, "type": platform, "message": message,
                "sourceID": source_id, "platformConfig": platform_config or {}}
        if port:
            body["port"] = port
        if args:
            body["args"] = args
        if envs:
            body["envs"] = envs
        return await self._req("POST", f"/v2/projects/{app}/releases", json=body)

    async def get_release(self, release_id: str) -> dict:
        data = await self._req("GET", f"/v1/releases/{release_id}")
        return data.get("release", data)

    async def build_logs(self, release_id: str, since: str | None = None) -> dict:
        return await self._req("GET", f"/v2/releases/{release_id}/build-logs",
                               params={"since": since} if since else None)

    async def list_releases(self, app: str, count: int = 10) -> list[dict]:
        data = await self._req("GET", f"/v1/projects/{app}/releases", params={"page": 1, "count": count})
        return data.get("releases", [])

    async def rollback(self, app: str, release_id: str) -> dict:
        try:
            return await self._req("POST", f"/v2/projects/{app}/releases/{release_id}/rollback")
        except LiaraError as e:
            if e.status in (404, 405):
                raise Limitation("API عمومی لیارا endpoint مستقیم rollback ندارد؛ "
                                 "باید سورس نسخه‌ی سالم قبلی دوباره دیپلوی شود.")
            raise

    # ------------------------------------------------------------ observability
    async def logs(self, app: str, minutes: int = 30, limit: int = 300) -> list[dict]:
        """GET /v2/projects/{app}/logs (liara-cli). Falls back to documented v1 endpoint."""
        start = int(time.time()) - minutes * 60
        lines: list[dict] = []
        try:
            data = await self._req("GET", f"/v2/projects/{app}/logs",
                                   params={"start": start, "direction": "forward", "last_lines": "false"})
            for entry in data.get("data", []) or []:
                rid = (entry.get("metaData") or {}).get("releaseId")
                for ts, line in entry.get("values", []):
                    lines.append({"ts": ts, "line": line, "releaseId": rid})
        except LiaraError as e:
            if e.status in (401, 403):
                raise
            data = await self._req("GET", f"/v1/projects/{app}/logs", params={"since": start})
            for x in data if isinstance(data, list) else data.get("data", []):
                lines.append({"ts": x.get("datetime"), "line": x.get("message", ""), "releaseId": None})
        lines.sort(key=lambda x: str(x["ts"]))
        return lines[-limit:]

    async def events(self, app: str, minutes: int = 180) -> list[str]:
        """GET /v1/projects/{name}/events — platform events (OOM kills, restarts, deploys...)."""
        start = int(time.time()) - minutes * 60
        data = await self._req("GET", f"/v1/projects/{app}/events", params={"start": start})
        out = []
        for entry in data.get("data", []) or []:
            for v in entry.get("values", []) or []:
                out.append(" ".join(str(x) for x in v[1:]) if isinstance(v, list) and len(v) > 1 else str(v))
        return out[-50:]

    async def metrics(self, app: str) -> dict:
        """GET /v1/projects/{name}/metrics/summary -> {cpuUsage:[{value:[ts,v]}], memoryUsage:[...], disksUsage:[...]}.
        Converted to % of the app's plan limits."""
        raw = None
        for base in (self.base, config.LIARA_METRICS_BASE.rstrip("/")):
            try:
                raw = await self._req("GET", f"{base}/v1/projects/{app}/metrics/summary")
                break
            except LiaraError as e:
                if e.status in (401,):
                    raise
                last = e
        if raw is None:
            raise Limitation("متریک‌های CPU/RAM از API لیارا برای این اپ در دسترس نبود "
                             f"({last.human()}). تحلیل با لاگ، رویدادها و تست مستقیم HTTP ادامه می‌یابد.")
        plan = {}
        try:
            app_d = await self.get_app(app)
            plan = (await self.list_plans()).get(app_d.get("planID"), {})
        except Exception:
            pass
        return {"metrics": normalize_metrics(raw, plan), "raw": raw}

    # ---------------------------------------------------------------- domains
    async def list_domains(self, app: str) -> list[dict]:
        data = await self._req("GET", "/v1/domains", params={"project": app})
        return data.get("domains", data if isinstance(data, list) else [])

    async def add_domain(self, app: str, domain: str) -> dict:
        return await self._req("POST", "/v1/domains", json={"name": domain, "project": app, "type": "PROJECT"})

    async def check_domain(self, domain_id: str) -> dict:
        return await self._req("GET", f"/v1/domains/{domain_id}/check")

    # ================================================================ IaaS (VMs)
    # developers.liara.ir/pages/iaas.md — base https://iaas-api.liara.ir
    async def vm_plans(self) -> dict:
        data = await self._req("GET", f"{IAAS_BASE}/plans")
        return data.get("plans", data)

    async def vm_os_list(self) -> dict:
        return await self._req("GET", f"{IAAS_BASE}/oss")

    async def vm_list(self) -> list[dict]:
        data = await self._req("GET", f"{IAAS_BASE}/vm")
        return data.get("vms", data if isinstance(data, list) else [])

    async def vm_get(self, vm_id: str) -> dict:
        data = await self._req("GET", f"{IAAS_BASE}/vm/{vm_id}")
        return data.get("vm", data)

    async def vm_find(self, name_or_id: str) -> dict:
        for v in await self.vm_list():
            if name_or_id in (v.get("_id"), v.get("name")):
                return v
        raise LiaraError(404, f"سرور «{name_or_id}» پیدا نشد", "vm")

    async def vm_create(self, name: str, os: str, plan: str, ssh_keys: list[str] | None = None) -> dict:
        body: dict = {"name": name, "OS": os, "plan": plan}
        if ssh_keys:
            body["config"] = {"SSHKeys": ssh_keys}
        return await self._req("POST", f"{IAAS_BASE}/vm", json=body)

    async def vm_power(self, vm_id: str, action: str) -> dict:
        return await self._req("PATCH", f"{IAAS_BASE}/vm/power/{vm_id}", json={"action": action})

    async def vm_resize(self, vm_id: str, plan: str) -> dict:
        return await self._req("PATCH", f"{IAAS_BASE}/vm/resize/{vm_id}", json={"plan": plan})

    async def vm_reset_password(self, vm_id: str) -> dict:
        return await self._req("POST", f"{IAAS_BASE}/vm/reset-password/{vm_id}")

    async def vm_operations(self, vm_id: str) -> list[dict]:
        data = await self._req("GET", f"{IAAS_BASE}/vm/operation/{vm_id}")
        return data.get("operations", [])

    async def vm_delete(self, vm_id: str) -> dict:
        return await self._req("DELETE", f"{IAAS_BASE}/vm/{vm_id}")


# ------------------------------------------------------------------ helpers
def _last_value(series) -> float | None:
    """series = [{"value": [ts, v], "applet": ...}, ...] -> sum of latest values across applets."""
    total, found = 0.0, False
    for s in series or []:
        val = s.get("value") if isinstance(s, dict) else None
        if isinstance(val, list) and val:
            v = val[-1]
            if isinstance(v, list):
                v = v[-1]
            try:
                total += float(v)
                found = True
            except (TypeError, ValueError):
                pass
    return total if found else None


def normalize_metrics(raw, plan: dict | None = None) -> dict:
    """Convert Liara's metrics summary into % of plan limits."""
    plan = plan or {}
    out: dict = {}
    if isinstance(raw, dict) and ("cpuUsage" in raw or "memoryUsage" in raw):
        cpu, mem = _last_value(raw.get("cpuUsage")), _last_value(raw.get("memoryUsage"))
        cpu_lim = (plan.get("CPU") or {}).get("amount") if isinstance(plan.get("CPU"), dict) else None
        ram_lim = (plan.get("RAM") or {}).get("amount") if isinstance(plan.get("RAM"), dict) else None
        if cpu is not None:
            out["cpu_raw"] = cpu
            pct = cpu / cpu_lim * 100 if cpu_lim else (cpu * 100 if cpu <= 1 else cpu)
            out["cpu"] = round(min(pct, 100), 1)
        if mem is not None:
            out["memory_raw"] = mem
            gb = mem / 1024 ** 3 if mem > 1e6 else (mem / 1024 if mem > 64 else mem)
            out["memory_mb"] = round(gb * 1024)
            if ram_lim:
                out["memory"] = round(min(gb / ram_lim * 100, 100), 1)
            elif mem <= 1:
                out["memory"] = round(mem * 100, 1)
        for d in raw.get("disksUsage") or []:
            out.setdefault("disks", []).append({"name": d.get("name"), "usage": d.get("usage"), "size": d.get("size")})
        return out
    # unknown shape -> best effort
    vals = dict(_walk(raw))
    for name, words in (("cpu", ("cpu",)), ("memory", ("mem", "ram"))):
        used = next((v for k, v in vals.items() if any(w in k for w in words)), None)
        if used is not None:
            out[name] = round(used * 100, 1) if used <= 1 else (used if used <= 100 else None)
    return out


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}".lower())
    elif isinstance(obj, list) and obj and isinstance(obj[-1], (int, float)):
        yield path, obj[-1]
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, obj


def plan_price_month(p: dict) -> float:
    """App plans expose hourly `price` (CLI multiplies by 720); VM plans expose monthlyPrice."""
    if not isinstance(p, dict):
        return 0
    if p.get("monthlyPrice"):
        return float(p["monthlyPrice"])
    if isinstance(p.get("price"), (int, float)):
        return float(p["price"]) * 720
    return 0


def app_url(app: str) -> str:
    return f"https://{app}.liara.run"
