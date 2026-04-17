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
