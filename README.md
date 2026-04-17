# ha-mcp-bridge

MCP server exposing Home Assistant + InfluxDB to Claude via stdio. Observation-first,
with scoped actuation gated behind an opt-in allowlist.

Tools:

**Observation**
- `ha_list_entities(domain=None)` — list entities, optional domain filter.
- `ha_state(entity_id)` — current state + attributes for one entity.
- `ha_history(entity_id, hours=24)` — state-change history over a window.
- `ha_query_grouped(entity_ids, hours=1)` — bundled history for correlation queries.
- `ha_history_binned(entity_id, hours=24, bin_minutes=15, aggregation="mean")` — bucketed aggregation.
- `ha_template(template)` — render a Jinja2 template against live HA state.
- `influx_flux(query)` — raw Flux query against the homelab InfluxDB 2.x instance.

**Actuation** (narrow)
- `ha_press_button(entity_id)` — press a `button.*` entity (ESPHome restart, etc.).
- `ha_call_service(domain, service, data)` — call any HA service that matches the env
  allowlist. Default-deny: the tool refuses unless both `HA_MCP_ALLOWED_SERVICES` and
  `HA_MCP_ALLOWED_ENTITY_PATTERNS` are set.

## Install

```bash
cd ~/git/ha-mcp-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Or with uv:

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Configure

1. Generate a long-lived access token in HA: **Settings → Profile → Security → Long-Lived Access Tokens**.
2. Copy `.env.example` to `.env` and paste the token.
3. For deployed use, store the token in Infisical and have the Infisical agent sync `.env` at runtime (same pattern as Kobold/ESP Forge).

```bash
cp .env.example .env
$EDITOR .env   # paste HA_TOKEN
```

### Env vars

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `HA_URL` | yes | `http://192.168.8.127:8123` | HA base URL |
| `HA_TOKEN` | yes | — | Long-lived access token |
| `HA_HTTP_TIMEOUT` | no | `15` | HA HTTP timeout (sec) |
| `HA_HISTORY_MAX_HOURS` | no | `168` | Upper clamp for history windows |
| `INFLUX_URL` | for `influx_flux` | — | InfluxDB 2.x base URL |
| `INFLUX_TOKEN` | for `influx_flux` | — | InfluxDB read token |
| `INFLUX_ORG` | for `influx_flux` | `homelab` | InfluxDB org |
| `HA_MCP_MAX_RESPONSE_BYTES` | no | `120000` | Per-tool response cap (stdio pipe-overflow guard) |
| `HA_MCP_DEBUG_LOG` | no | — | If set, bridge writes DEBUG logs to this file |
| `HA_MCP_ALLOWED_SERVICES` | to enable `ha_call_service` | — | fnmatch globs on `domain.service` |
| `HA_MCP_ALLOWED_ENTITY_PATTERNS` | to enable `ha_call_service` | — | fnmatch globs on `entity_id` |

### Scoped `ha_call_service` — allowlist examples

Plant shelf only, safe actuation:

```bash
HA_MCP_ALLOWED_SERVICES=light.turn_on,light.turn_off,light.toggle,switch.turn_on,switch.turn_off,switch.toggle,fan.*,scene.turn_on
HA_MCP_ALLOWED_ENTITY_PATTERNS=*plant_shelf*,light.grow_*,switch.grow_*,fan.grow_*
```

Semantics:
- Both lists must be non-empty — empty = tool disabled (default-deny).
- Service must match at least one pattern in `HA_MCP_ALLOWED_SERVICES`.
- Every `entity_id` in the call payload must match at least one pattern in `HA_MCP_ALLOWED_ENTITY_PATTERNS`.
- Area/device-level targeting is refused — callers must pass explicit `entity_id`.
- Every denial is logged to stderr for audit.

## Run

```bash
ha-mcp-bridge
```

Or directly:

```bash
python -m ha_mcp_bridge.server
```

The server speaks MCP over stdio. Wire it into a Claude client via its MCP config (e.g. Claude Desktop `claude_desktop_config.json`, or `~/.claude/settings.json` `mcpServers`).

### Claude Desktop / Claude Code example

```json
{
  "mcpServers": {
    "ha": {
      "command": "/home/jay/git/ha-mcp-bridge/.venv/bin/ha-mcp-bridge"
    }
  }
}
```

## Sanity-check without MCP

The `ha_client.py` module can be exercised directly:

```python
import asyncio, os
from ha_mcp_bridge.ha_client import HAClient

async def main():
    async with HAClient(os.environ["HA_URL"], os.environ["HA_TOKEN"]) as ha:
        states = await ha.list_states()
        print(f"entities: {len(states)}")
        sample = next(s for s in states if s["entity_id"].startswith("sensor.plant_shelf_temps"))
        print(sample)

asyncio.run(main())
```

## Design notes

- **Observation-first, scoped actuation.** The user retains physical control; Claude gets broad observation and narrow, env-gated actuation. `ha_call_service` is disabled by default and requires an explicit allowlist on two axes (service + entity) before it will do anything.
- **Per-call session.** Each tool opens its own `aiohttp.ClientSession`. HA's local API is cheap enough that connection pooling wouldn't pay for the added lifecycle complexity.
- **`minimal_response`** is set on history calls so payload tracks change frequency, not polling rate — a 10s-interval sensor with stable readings returns a few rows, not 360.
- **Errors surface in-band.** Tool results include `{"error": "..."}` instead of raising — keeps MCP client behavior predictable and lets Claude reason about outages.
- **Stdio-overflow guard.** Responses are capped at ~120KB serialized; oversize responses return an `error: "response_too_large"` dict instead of blocking the stdio pipe.
