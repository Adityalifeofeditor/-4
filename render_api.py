"""
Updated Render API async wrapper using httpx.

- Robust request wrapper returning (ok: bool, data: Any)
- Correct parsing for owners, services, deploys, logs
- Safe defaults if fields missing
- Supports service creation, update, deploy, restart
- Env vars: list/upsert/delete
- Works with Render v1 API
"""
from typing import Any, Dict, Optional, Tuple
import httpx
import logging

logger = logging.getLogger("render_api")
logger.setLevel(logging.INFO)

BASE = "https://api.render.com/v1/services?limit=3"

VALID_SERVICE_TYPES = {
    "static_site",
    "web_service",
    "private_service",
    "background_worker",
    "cron_job",
    "workflow",
}


class RenderAPI:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Any = None,
        timeout: int = 30
    ) -> Tuple[bool, Any]:
        url = BASE + path
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if json_data is not None:
                    resp = await client.request(method, url, params=params, json=json_data, headers=self._headers())
                else:
                    resp = await client.request(method, url, params=params, headers=self._headers())
        except httpx.RequestError as e:
            logger.exception("Request error %s %s", method, url)
            return False, {"error": str(e)}

        try:
            data = resp.json()
        except Exception:
            data = resp.text

        if 200 <= resp.status_code < 300:
            return True, data
        else:
            logger.debug("Render API error %s %s -> %s", resp.status_code, url, data)
            return False, {"status_code": resp.status_code, "body": data}

    # ----------------- Owners -----------------
    async def owners(self) -> Tuple[bool, Any]:
        ok, data = await self._request("GET", "/owners")
        if not ok:
            return False, data
        if isinstance(data, list):
            # normalize owner object
            return True, [{"owner": d.get("owner", {})} for d in data]
        return True, data

    async def resolve_owner_id(self) -> Tuple[Optional[str], Any]:
        ok, data = await self.owners()
        if not ok or not isinstance(data, list):
            return None, data
        # prefer team
        for item in data:
            owner = item.get("owner", {})
            if owner.get("type") == "team":
                return owner.get("id"), data
        for item in data:
            owner = item.get("owner", {})
            if owner.get("type") == "user":
                return owner.get("id"), data
        return None, data

    # ----------------- Services -----------------
    async def list_services(self, limit: int = 50) -> Tuple[bool, Any]:
        limit = min(int(limit), 100)
        return await self._request("GET", "/services", params={"limit": limit})

    async def get_service(self, service_id: str) -> Tuple[bool, Any]:
        ok, data = await self._request("GET", f"/services/{service_id}")
        if not ok:
            return False, data
        # normalize nested serviceDetails
        if "serviceDetails" in data:
            data["status"] = data.get("serviceDetails", {}).get("status", data.get("status"))
            data["defaultDomain"] = data.get("serviceDetails", {}).get("defaultDomain", data.get("defaultDomain"))
        return True, data

    async def create_service(
        self,
        owner_id: str,
        name: str,
        service_type: str,
        repo: Optional[str] = None,
        branch: str = "main",
        runtime: Optional[str] = None,
        start_command: Optional[str] = None,
        build_command: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        plan: Optional[str] = None
    ) -> Tuple[bool, Any]:
        if service_type not in VALID_SERVICE_TYPES:
            return False, {"status_code": 400, "body": {"message": f"invalid service type: {service_type}"}}

        body: Dict[str, Any] = {"ownerId": owner_id, "name": name, "type": service_type}

        repo_types = {"web_service", "private_service", "background_worker", "workflow"}
        if service_type in repo_types and repo:
            body["repo"] = repo
            body["branch"] = branch
            if runtime: body["runtime"] = runtime
            if start_command: body["startCommand"] = start_command
            if build_command: body["buildCommand"] = build_command
            if env_vars:
                body["envVars"] = [{"key": k, "value": v} for k, v in env_vars.items()]

        if service_type == "static_site" and repo:
            body["repo"] = repo
            body["branch"] = branch
            if build_command: body["buildCommand"] = build_command

        if plan: body["plan"] = plan

        return await self._request("POST", "/services", json_data=body)

    async def update_service(self, service_id: str, update_fields: Dict[str, Any]) -> Tuple[bool, Any]:
        return await self._request("PATCH", f"/services/{service_id}", json_data=update_fields)

    # ----------------- Deploy / Restart -----------------
    async def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        return await self._request("POST", f"/services/{service_id}/deploys", json_data={"clearCache": clear_cache})

    async def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        ok, res = await self._request("POST", f"/services/{service_id}/restart", json_data={})
        if ok:
            return True, res
        # fallback: redeploy
        return await self.trigger_deploy(service_id)

    # ----------------- Logs -----------------
    async def get_service_logs(self, service_id: str, tail: bool = True, limit: int = 100) -> Tuple[bool, Any]:
        limit = min(int(limit), 100)
        params = {"tail": "true" if tail else "false", "limit": limit}
        return await self._request("GET", f"/services/{service_id}/logs", params=params)

    async def list_logs(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None
    ) -> Tuple[bool, Any]:
        params: Dict[str, Any] = {}
        if start_time: params["startTime"] = start_time
        if end_time: params["endTime"] = end_time
        if cursor: params["cursor"] = cursor
        if limit: params["limit"] = min(int(limit), 100) if limit else None
        return await self._request("GET", "/logs", params=params)

    # ----------------- Env Vars -----------------
    async def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request("GET", f"/services/{service_id}/env-vars")

    async def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        body = [{"key": k, "value": v} for k, v in kv.items()]
        return await self._request("PUT", f"/services/{service_id}/env-vars", json_data=body)

    async def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        return await self._request("DELETE", f"/services/{service_id}/env-vars/{key_name}")
