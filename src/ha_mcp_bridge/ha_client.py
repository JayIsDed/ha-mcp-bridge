from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp


class HAError(Exception):
    """Raised when Home Assistant returns an error or is unreachable."""


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "HAClient":
        self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        assert self._session is not None, "HAClient must be used as async context manager"
        url = f"{self._base}{path}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 401:
                    raise HAError("Unauthorized — check HA_TOKEN (expired or invalid).")
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = await resp.text()
                    raise HAError(f"HA {resp.status}: {body[:200]}")
                return await resp.json()
        except aiohttp.ClientConnectorError as e:
            raise HAError(f"Cannot reach HA at {self._base}: {e}") from e
        except aiohttp.ClientError as e:
            raise HAError(f"HTTP error talking to HA: {e}") from e

    async def _get_bytes(self, path: str, params: dict[str, str] | None = None) -> bytes:
        assert self._session is not None, "HAClient must be used as async context manager"
        url = f"{self._base}{path}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 401:
                    raise HAError("Unauthorized — check HA_TOKEN (expired or invalid).")
                if resp.status == 404:
                    raise HAError(f"HA endpoint or entity not found: {path}")
                if resp.status >= 400:
                    body = await resp.text()
                    raise HAError(f"HA {resp.status}: {body[:200]}")
                return await resp.read()
        except aiohttp.ClientConnectorError as e:
            raise HAError(f"Cannot reach HA at {self._base}: {e}") from e
        except aiohttp.ClientError as e:
            raise HAError(f"HTTP error talking to HA: {e}") from e

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        assert self._session is not None, "HAClient must be used as async context manager"
        url = f"{self._base}{path}"
        try:
            async with self._session.post(url, json=body) as resp:
                if resp.status == 401:
                    raise HAError("Unauthorized — check HA_TOKEN (expired or invalid).")
                if resp.status == 404:
                    raise HAError(f"HA endpoint not found: {path}")
                if resp.status >= 400:
                    text = await resp.text()
                    raise HAError(f"HA {resp.status}: {text[:200]}")
                ctype = resp.headers.get("content-type", "")
                if "application/json" in ctype:
                    return await resp.json()
                return await resp.text()
        except aiohttp.ClientConnectorError as e:
            raise HAError(f"Cannot reach HA at {self._base}: {e}") from e
        except aiohttp.ClientError as e:
            raise HAError(f"HTTP error talking to HA: {e}") from e

    async def list_states(self) -> list[dict[str, Any]]:
        data = await self._get("/api/states")
        return data if isinstance(data, list) else []

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        return await self._get(f"/api/states/{entity_id}")

    async def get_history(
        self,
        entity_id: str,
        hours: int,
        minimal_response: bool = True,
    ) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        params: dict[str, str] = {
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
        }
        if minimal_response:
            params["minimal_response"] = "true"
        path = f"/api/history/period/{start.isoformat()}"
        data = await self._get(path, params=params)
        if not data or not isinstance(data, list):
            return []
        # HA returns list-of-lists: outer per entity (1 here), inner time-series
        return data[0] if data and isinstance(data[0], list) else []

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """POST /api/services/<domain>/<service>. Returns the updated states list HA emits."""
        body = dict(data or {})
        return await self._post(f"/api/services/{domain}/{service}", body)

    async def render_template(self, template: str) -> str:
        """POST /api/template. Returns rendered text (plain, not JSON)."""
        result = await self._post("/api/template", {"template": template})
        # Template endpoint returns plain text even with JSON content-type sometimes.
        return str(result)

    async def get_camera_snapshot(self, entity_id: str) -> bytes:
        """GET /api/camera_proxy/<entity_id>. Returns raw image bytes (usually JPEG)."""
        return await self._get_bytes(f"/api/camera_proxy/{entity_id}")

    async def get_logbook(
        self,
        hours: int,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/logbook/<start_iso>. Human-readable event stream."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        params: dict[str, str] = {"end_time": end.isoformat()}
        if entity_id:
            params["entity"] = entity_id
        path = f"/api/logbook/{start.isoformat()}"
        data = await self._get(path, params=params)
        return data if isinstance(data, list) else []
