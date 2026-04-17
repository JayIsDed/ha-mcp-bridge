# ha-mcp-bridge

Read-only MCP server exposing Home Assistant entity state + history to Claude via stdio.

Four tools:

- `ha_list_entities(domain=None)` — list entities, optional domain filter.
- `ha_state(entity_id)` — current state + attributes for one entity.
- `ha_history(entity_id, hours=24)` — state-change history over a window.
- `ha_query_grouped(entity_ids, hours=1)` — bundled history for correlation queries.

Observation rights only. No service calls. No actuation. Add those later if you want.

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

- **Read-only on purpose.** Matches the collaborative-loop-protocol split: user retains physical control, Claude gets observation rights. Adding service calls later is trivial but a deliberate decision, not a drift.
- **Per-call session.** Each tool opens its own `aiohttp.ClientSession`. HA's local API is cheap enough that connection pooling wouldn't pay for the added lifecycle complexity.
- **`minimal_response`** is set on history calls so payload tracks change frequency, not polling rate — a 10s-interval sensor with stable readings returns a few rows, not 360.
- **Errors surface in-band.** Tool results include `{"error": "..."}` instead of raising — keeps MCP client behavior predictable and lets Claude reason about outages.
