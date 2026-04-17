from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .ha_client import HAClient, HAError
from .types import EntityInfo, EntityState, HistoryPoint


def _load_env() -> None:
    # Honor a .env next to the project if present; real deploys use Infisical-synced env.
    here = Path(__file__).resolve().parent.parent.parent
    load_dotenv(here / ".env", override=False)
    load_dotenv(override=False)


_load_env()

HA_URL = os.environ.get("HA_URL", "http://192.168.8.127:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TIMEOUT = float(os.environ.get("HA_HTTP_TIMEOUT", "15"))
HA_HISTORY_MAX_HOURS = int(os.environ.get("HA_HISTORY_MAX_HOURS", "168"))

if not HA_TOKEN:
    # Don't crash at import — MCP handshake may still work, but tool calls will fail loud.
    # This lets `ha-mcp-bridge --help` style probes work without a token present.
    import sys

    print(
        "WARN: HA_TOKEN not set. Tool calls will fail until it is populated.",
        file=sys.stderr,
    )


mcp = FastMCP("ha-mcp-bridge")


def _client() -> HAClient:
    return HAClient(HA_URL, HA_TOKEN, timeout=HA_TIMEOUT)


@mcp.tool()
async def ha_list_entities(domain: str | None = None) -> list[dict]:
    """List Home Assistant entities, optionally filtered by domain.

    Args:
        domain: Optional domain prefix filter (e.g. "sensor", "switch", "binary_sensor",
            "light", "climate"). When omitted, returns every entity known to HA.

    Returns:
        List of {entity_id, friendly_name, state, last_changed, unit_of_measurement,
        device_class}. Compact shape so Claude can scan many entries without flooding context.
    """
    async with _client() as ha:
        try:
            raw = await ha.list_states()
        except HAError as e:
            return [{"error": str(e)}]

    if domain:
        prefix = f"{domain}."
        raw = [e for e in raw if e.get("entity_id", "").startswith(prefix)]

    return [EntityInfo.from_ha(e).model_dump() for e in raw]


@mcp.tool()
async def ha_state(entity_id: str) -> dict:
    """Get the current state and attributes of a single Home Assistant entity.

    Args:
        entity_id: Fully-qualified entity id, e.g. "sensor.plant_shelf_temps_tank_center".

    Returns:
        {entity_id, state, attributes, last_changed, last_updated}. Attributes include
        unit_of_measurement, friendly_name, device_class, etc. depending on entity type.
        Returns {error: ...} if the entity is not found or HA is unreachable.
    """
    async with _client() as ha:
        try:
            raw = await ha.get_state(entity_id)
        except HAError as e:
            return {"error": str(e)}

    if raw is None:
        return {"error": f"entity not found: {entity_id}"}
    return EntityState.from_ha(raw).model_dump()


@mcp.tool()
async def ha_history(entity_id: str, hours: int = 24) -> list[dict]:
    """Fetch state-change history for an entity over a time window.

    Args:
        entity_id: Fully-qualified entity id.
        hours: Window length in hours, 1..168 (1h to 1 week). Clamped if out of range.

    Returns:
        Time-ordered list of {state, last_changed, unit_of_measurement}. Uses HA's
        minimal_response mode so payload scales with change frequency, not sample rate.
        Returns [{error: ...}] on failure.
    """
    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))
    async with _client() as ha:
        try:
            raw = await ha.get_history(entity_id, hours)
        except HAError as e:
            return [{"error": str(e)}]

    return [HistoryPoint.from_ha(p).model_dump() for p in raw]


@mcp.tool()
async def ha_query_grouped(entity_ids: list[str], hours: int = 1) -> dict:
    """Fetch history for multiple entities in one call. Useful for correlated analysis.

    Args:
        entity_ids: List of fully-qualified entity ids.
        hours: Window length in hours, 1..168.

    Returns:
        {entity_id: [HistoryPoint, ...]} keyed by entity. Missing/errored entities
        show up with a single {error: ...} entry in their list. Issues requests
        concurrently, so wall-clock cost scales with slowest entity, not sum.
    """
    import asyncio

    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))

    async def one(ha: HAClient, eid: str) -> tuple[str, list[dict]]:
        try:
            raw = await ha.get_history(eid, hours)
            return eid, [HistoryPoint.from_ha(p).model_dump() for p in raw]
        except HAError as e:
            return eid, [{"error": str(e)}]

    async with _client() as ha:
        results = await asyncio.gather(*(one(ha, eid) for eid in entity_ids))

    return dict(results)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
