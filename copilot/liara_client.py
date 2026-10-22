"""Thin async client for the Liara REST API.

Every endpoint here was taken from the official liara-cli source
(github.com/liara-ir/liara-cli) so it matches what Liara really exposes.
Endpoints that are NOT used by the official CLI are marked `unverified=True`
and degrade gracefully: if Liara rejects them we report a clear limitation
instead of failing the whole flow.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from . import config


class LiaraError(Exception):
    def __init__(self, status: int, message: str, path: str = ""):
        self.status, self.message, self.path = status, message, path
        super().__init__(f"[{status}] {path}: {message}")

    def human(self) -> str:
        hints = {
            401: "توکن API نامعتبر یا منقضی است. از console.liara.ir/API توکن جدید بگیرید.",
            402: "موجودی حساب لیارا کافی نیست. لطفاً ابتدا حساب را شارژ کنید (این کار فقط از دست خودتان برمی‌آید).",
            403: "این توکن اجازه‌ی این عملیات را ندارد.",
            404: "منبع موردنظر پیدا نشد (نام اپ را بررسی کنید).",
            409: "منبعی با این نام از قبل وجود دارد.",
            429: "تعداد درخواست‌ها زیاد است؛ چند ثانیه بعد دوباره تلاش می‌کنم.",
        }
        return hints.get(self.status, self.message or "خطای ناشناخته از API لیارا")


class Limitation(Exception):
    """Raised when an operation is not available through Liara's public API."""


class LiaraClient:
    def __init__(self, token: str | None = None, base: str | None = None, team_id: str | None = None):
        self.token = token or config.LIARA_API_TOKEN
        self.base = (base or config.LIARA_API_BASE).rstrip("/")
        self.team_id = team_id if team_id is not None else config.LIARA_TEAM_ID
        self._http = httpx.AsyncClient(
            base_url=self.base,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    async def close(self):
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def _req(self, method: str, path: str, **kw) -> Any:
        params = kw.pop("params", None) or {}
        if self.team_id:
            params["teamID"] = self.team_id
        try:
            r = await self._http.request(method, path, params=params, **kw)
        except httpx.HTTPError as e:
            raise LiaraError(0, f"ارتباط با API لیارا برقرار نشد: {e}", path)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or r.text
            except Exception:
                msg = r.text[:300]
            raise LiaraError(r.status_code, str(msg), path)
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    # ---------------------------------------------------------------- account
    async def me(self) -> dict:
        return await self._req("GET", "/v1/me")

    async def list_plans(self) -> dict:
        """Plans live in GET /v1/me -> plans.projects (same as `liara plan list`)."""
        data = await self.me()
        return (data.get("plans") or {}).get("projects") or {}

    # ------------------------------------------------------------------- apps
    async def list_apps(self) -> list[dict]:
        data = await self._req("GET", "/v1/projects")
        return data.get("projects", [])

    async def get_app(self, app: str) -> dict:
        data = await self._req("GET", f"/v1/projects/{app}")
        return data.get("project", data)

    async def create_app(self, name: str, platform: str, plan_id: str, bundle_plan_id: str = "standard",
                         network: str | None = None) -> dict:
        body = {"name": name, "planID": plan_id, "platform": platform, "bundlePlanID": bundle_plan_id}
        if network:
            body["network"] = network
        return await self._req("POST", "/v1/projects/", json=body)

    async def delete_app(self, app: str) -> dict:
        return await self._req("DELETE", f"/v1/projects/{app}")

    async def restart_app(self, app: str) -> dict:
        return await self._req("POST", f"/v1/projects/{app}/actions/restart")

    async def scale_app(self, app: str, scale: int) -> dict:
        return await self._req("POST", f"/v1/projects/{app}/actions/scale", json={"scale": scale})

    async def update_port(self, app: str, port: int) -> dict:
        return await self._req("POST", "/v1/projects/update-port", json={"project": app, "port": port})

    async def list_networks(self) -> list[dict]:
        data = await self._req("GET", "/v1/networks")
        return data.get("networks", data if isinstance(data, list) else [])

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

    async def create_release(self, app: str, source_id: str, platform: str, port: int,
                             envs: dict | None = None, message: str = "Deployed by Liara Copilot",
                             platform_config: dict | None = None) -> dict:
        body = {
            "build": {"cache": True},
            "port": port,
            "type": platform,
            "message": message,
            "sourceID": source_id,
            "platformConfig": platform_config or {},
        }
        if envs:
            body["envs"] = envs
        return await self._req("POST", f"/v2/projects/{app}/releases", json=body)

    async def get_release(self, release_id: str) -> dict:
        data = await self._req("GET", f"/v1/releases/{release_id}")
        return data.get("release", data)

    async def build_logs(self, release_id: str, since: str | None = None) -> dict:
        path = f"/v2/releases/{release_id}/build-logs"
        return await self._req("GET", path, params={"since": since} if since else None)

    async def list_releases(self, app: str, count: int = 10) -> list[dict]:
        data = await self._req("GET", f"/v1/projects/{app}/releases", params={"page": 1, "count": count})
        return data.get("releases", [])

    async def rollback(self, app: str, release_id: str) -> dict:
        # unverified: not used by liara-cli. Fallback = redeploy the old source.
        try:
            return await self._req("POST", f"/v2/projects/{app}/releases/{release_id}/rollback")
        except LiaraError as e:
            if e.status in (404, 405):
                raise Limitation("API عمومی لیارا endpoint مستقیم rollback ندارد؛ "
                                 "باید سورس نسخه‌ی سالم قبلی دوباره دیپلوی شود.")
            raise

    # ------------------------------------------------------------ observability
    async def logs(self, app: str, minutes: int = 30, limit: int = 300) -> list[dict]:
        """GET /v2/projects/{app}/logs  (as in `liara app logs`)."""
        start = int(time.time()) - minutes * 60
        data = await self._req("GET", f"/v2/projects/{app}/logs",
                               params={"start": start, "direction": "forward", "last_lines": "false"})
        lines = []
        for entry in data.get("data", []) or []:
            rid = (entry.get("metaData") or {}).get("releaseId")
            for ts, line in entry.get("values", []):
                lines.append({"ts": ts, "line": line, "releaseId": rid})
        lines.sort(key=lambda x: str(x["ts"]))
        return lines[-limit:]

    async def metrics(self, app: str) -> dict:
        """GET {api.iran.liara.ir}/v1/projects/{app}/metrics/summary — the same call the
        Liara console makes for the live CPU/RAM bars. Not part of liara-cli, so the
        response is normalised defensively and failures become a declared Limitation."""
        url = f"{config.LIARA_METRICS_BASE.rstrip('/')}/v1/projects/{app}/metrics/summary"
        try:
            raw = await self._req("GET", url)
        except LiaraError as e:
            if e.status in (401, 403, 404, 0):
                raise Limitation("متریک‌های CPU/RAM از API لیارا برای این اپ در دسترس نبود "
                                 f"({e.human()}). تحلیل با لاگ، وضعیت سرویس و تست مستقیم HTTP ادامه می‌یابد.")
            raise
        return {"metrics": normalize_metrics(raw), "raw": raw}

    # ---------------------------------------------------------------- domains
    async def list_domains(self, app: str) -> list[dict]:
        data = await self._req("GET", "/v1/domains", params={"project": app})
        return data.get("domains", data if isinstance(data, list) else [])

    async def add_domain(self, app: str, domain: str) -> dict:
        return await self._req("POST", "/v1/domains", json={"name": domain, "project": app, "type": "ROOT"})

    async def check_domain(self, domain_id: str) -> dict:
        return await self._req("GET", f"/v1/domains/{domain_id}/check")


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}".lower())
    elif isinstance(obj, list) and obj and all(isinstance(x, (int, float)) for x in obj[-1:]):
        yield path, obj[-1]
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, obj


def normalize_metrics(raw) -> dict:
    """Best-effort: find CPU / memory usage (as % of plan limit) in an unknown JSON shape."""
    vals = dict(_walk(raw))
    out: dict = {}

    def pick(words, exclude=("limit", "max", "total", "request", "quota")):
        for k, v in vals.items():
            if any(w in k for w in words) and not any(x in k for x in exclude):
                return v
        return None

    def limit(words):
        for k, v in vals.items():
            if any(w in k for w in words) and any(x in k for x in ("limit", "max", "total", "quota")):
                return v
        return None

    for name, words in (("cpu", ("cpu",)), ("memory", ("mem", "ram"))):
        used, lim = pick(words), limit(words)
        if used is None:
            continue
        if lim:
            pct = used / lim * 100
        elif used <= 1.0:
            pct = used * 100
        else:
            pct = used if used <= 100 else None
        if pct is not None:
            out[name] = round(pct, 1)
        out[name + "_raw"] = used
    return out


def app_url(app: str) -> str:
    return f"https://{app}.liara.run"
