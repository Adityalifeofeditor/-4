"""
Robust Async Render.com v1 API Client using httpx
Features:
- Full error tracing with context
- Automatic owner resolution (team > user)
- Safe field access with defaults
- Comprehensive logging with tracebacks
- Proper base URL and request handling
"""

from typing import Any, Dict, Optional, Tuple, List
import httpx
import logging
import traceback
from contextlib import asynccontextmanager

# Configure logger
logger = logging.getLogger("render_api")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Correct base URL
BASE_URL = "https://api.render.com/v1"

VALID_SERVICE_TYPES = {
    "static_site",
    "web_service",
    "private_service",
    "background_worker",
    "cron_job",
    "workflow",
}


class RenderAPIError(Exception):
    """Custom exception with context"""
    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        self.message = message
        self.context = context or {}
        super().__init__(self.message)

    def __str__(self):
        ctx = " | ".join(f"{k}={v}" for k, v in self.context.items())
        return f"[RenderAPIError] {self.message}" + (f" | {ctx}" if ctx else "")


class RenderAPI:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 30):
        if not api_key:
            raise ValueError("Render API key is required")
        self.api_key = api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    @asynccontextmanager
    async def _get_client(self):
        """Reusable async client with proper cleanup"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        try:
            yield self._client
        except Exception as e:
            logger.error("Unexpected error in HTTP client: %s", e)
            raise

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Any]:
        url = f"{BASE_URL}{endpoint}"
        context = context or {}
        log_context = " | ".join(f"{k}={v}" for k, v in context.items())

        try:
            async with self._get_client() as client:
                logger.info("→ %s %s %s", method, endpoint, log_context or "")

                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=self._headers(),
                )

                # Try to parse JSON, fallback to text
                try:
                    data = response.json()
                except Exception as json_exc:
                    logger.warning("Failed to parse JSON response, using raw text: %s", json_exc)
                    data = response.text

                if 200 <= response.status_code < 300:
                    logger.debug("✓ Success %s %s", response.status_code, endpoint)
                    return True, data
                else:
                    error_msg = f"HTTP {response.status_code} from Render API"
                    logger.error("✗ %s | %s | Response: %s", log_context, error_msg, data)
                    return False, {
                        "status_code": response.status_code,
                        "error": error_msg,
                        "body": data,
                        "url": url,
                    }

        except httpx.RequestError as e:
            tb = traceback.format_exc()
            logger.error(
                "Network/request error during %s %s %s\n%s",
                method, endpoint, log_context, tb
            )
            return False, {
                "error": "Network error",
                "details": str(e),
                "type": e.__class__.__name__,
                "url": url,
            }
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(
                "Unexpected error in _request(%s %s %s):\n%s",
                method, endpoint, log_context, tb
            )
            raise RenderAPIError("Unexpected error in API call", {**context, "endpoint": endpoint}) from e

    # ==================== Owners ====================
    async def owners(self) -> Tuple[bool, List[Dict[str, Any]]]:
        ok, data = await self._request("GET", "/owners", context={"action": "list_owners"})
        if not ok:
            return False, data

        if isinstance(data, list):
            normalized = [{"owner": item.get("owner") or item} for item in data]
            return True, normalized
        return True, data

    async def resolve_owner_id(self) -> Tuple[Optional[str], Any]:
        ok, owners_data = await self.owners()
        if not ok or not isinstance(owners_data, list):
            return None, owners_data

        # Prefer team, then user
        for item in owners_data:
            owner = item.get("owner", {})
            if owner.get("type") == "team":
                logger.info("Using team owner: %s (%s)", owner.get("name"), owner.get("id"))
                return owner.get("id"), owners_data

        for item in owners_data:
            owner = item.get("owner", {})
            if owner.get("type") == "user":
                logger.info("Using personal owner: %s (%s)", owner.get("email"), owner.get("id"))
                return owner.get("id"), owners_data

        logger.warning("No valid owner found")
        return None, owners_data

    # ==================== Services ====================
    async def list_services(self, limit: int = 50) -> Tuple[bool, Any]:
        limit = min(max(int(limit), 1), 100)
        return await self._request(
            "GET", "/services", params={"limit": limit}, context={"action": "list_services", "limit": limit}
        )

    async def get_service(self, service_id: str) -> Tuple[bool, Dict[str, Any]]:
        ok, data = await self._request(
            "GET", f"/services/{service_id}", context={"service_id": service_id, "action": "get_service"}
        )
        if not ok:
            return False, data

        # Flatten serviceDetails if present
        if isinstance(data, dict) and "serviceDetails" in data:
            details = data["serviceDetails"]
            data.update({
                "status": details.get("status", data.get("status")),
                "url": details.get("url") or data.get("service", {}).get("url"),
                "suspenders": details.get("suspenders"),
            })
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
        plan: Optional[str] = None,
    ) -> Tuple[bool, Any]:
        if service_type not in VALID_SERVICE_TYPES:
            return False, {"error": f"Invalid service_type: {service_type}", "valid_types": list(VALID_SERVICE_TYPES)}

        body: Dict[str, Any] = {
            "ownerId": owner_id,
            "name": name,
            "type": service_type,
        }

        if repo:
            body["repo"] = repo
            body["branch"] = branch

            if service_type in {"web_service", "private_service", "background_worker", "workflow"}:
                if runtime:
                    body["runtime"] = runtime
                if start_command:
                    body["startCommand"] = start_command
                if build_command:
                    body["buildCommand"] = build_command
                if env_vars:
                    body["envVars"] = [{"key": k, "value": v} for k, v in env_vars.items()]

            elif service_type == "static_site":
                if build_command:
                    body["buildCommand"] = build_command

        if plan:
            body["plan"] = plan

        return await self._request(
            "POST",
            "/services",
            json_data=body,
            context={"action": "create_service", "name": name, "type": service_type},
        )

    async def update_service(self, service_id: str, update_fields: Dict[str, Any]) -> Tuple[bool, Any]:
        return await self._request(
            "PATCH",
            f"/services/{service_id}",
            json_data=update_fields,
            context={"service_id": service_id, "action": "update_service"},
        )

    # ==================== Deploy / Restart ====================
    async def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        return await self._request(
            "POST",
            f"/services/{service_id}/deploys",
            json_data={"clearCache": clear_cache},
            context={"service_id": service_id, "action": "trigger_deploy", "clear_cache": clear_cache},
        )

    async def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        ok, data = await self._request(
            "POST",
            f"/services/{service_id}/restart",
            json_data={},
            context={"service_id": service_id, "action": "restart_service"},
        )
        if ok:
            logger.info("Service restarted successfully: %s", service_id)
            return True, data
        else:
            logger.warning("Restart endpoint failed, falling back to redeploy: %s", service_id)
            return await self.trigger_deploy(service_id, clear_cache=True)

    # ==================== Logs ====================
    async def get_service_logs(
        self, service_id: str, tail: bool = True, limit: int = 100
    ) -> Tuple[bool, Any]:
        limit = min(int(limit), 1000)
        params = {"limit": limit, "tail": "true" if tail else "false"}
        return await self._request(
            "GET",
            f"/services/{service_id}/logs",
            params=params,
            context={"service_id": service_id, "action": "get_logs", "tail": tail, "limit": limit},
        )

    async def list_logs(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Tuple[bool, Any]:
        params: Dict[str, Any] = {}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = min(int(limit), 1000)

        return await self._request(
            "GET", "/logs", params=params or None, context={"action": "list_global_logs"}
        )

    # ==================== Environment Variables ====================
    async def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request(
            "GET",
            f"/services/{service_id}/env-vars",
            context={"service_id": service_id, "action": "list_env_vars"},
        )

    async def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        if not kv:
            return False, {"error": "No environment variables provided"}

        body = [{"key": k, "value": v} for k, v in kv.items()]
        return await self._request(
            "PUT",
            f"/services/{service_id}/env-vars",
            json_data=body,
            context={"service_id": service_id, "action": "upsert_env_vars", "count": len(kv)},
        )

    async def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        return await self._request(
            "DELETE",
            f"/services/{service_id}/env-vars/{key_name}",
            context={"service_id": service_id, "key": key_name, "action": "delete_env_var"},
        )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
