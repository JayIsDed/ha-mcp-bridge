# MCP Bridge Hang Debugging Scaffold

**Date:** 2026-04-17
**Session handoff:** main session flagged intermittent hangs; parallel debug session can work in isolation

## Symptoms

- Tool calls return `"Tool result missing due to internal error"` to Claude Code
- Affects multiple tools: `ha_state`, `ha_list_entities`, `ha_query_grouped`, `influx_flux`
- Not deterministic by tool — same tool sometimes works, sometimes hangs
- Same underlying resources (HA REST at `http://192.168.8.127:8123`, InfluxDB at `http://192.168.8.112:8086`) are reachable via `curl` from the same host
- Happens more often on calls expected to return large payloads

## Observed in current session

- `ha_query_grouped` over 10h with 5 entities returned ~359KB first time, then subsequent calls hung
- `ha_list_entities(domain="button")` hung (filed an Agent internal error)
- `influx_flux` with moderate aggregated query hung once
- Various `ha_state` calls hung intermittently

## Leading hypotheses (ordered by likelihood)

1. **Stdio response-size limit.** MCP over stdio is line-delimited JSON-RPC. Very large responses may exceed pipe buffers on the Python side or Claude Code's MCP client line-parser. If the response contains embedded `\n` or very long lines, chunking boundaries may split responses badly.

2. **Claude Code MCP client timeout shorter than HA/InfluxDB response time.** Bridge may still be processing when Claude times out. Bridge keeps going, but the response never reaches Claude because the client gave up. Next call sees the previous response still in-flight or stale.

3. **Silent async exception in FastMCP handler** that doesn't propagate through to a clean error response. The aiohttp session context manager could raise on exit if the event loop is closed wrong.

4. **Response serialization edge case** — pydantic model_dump on large nested structures may emit non-UTF8 or escaped characters that break stdio framing.

## Debugging steps

### Step 1: Add logging to stderr

The bridge must not log to stdout (that's the MCP transport). Add a logger writing to a file:

```python
# In server.py top
import logging
logging.basicConfig(
    filename="/tmp/ha-mcp-bridge-debug.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ha-mcp-bridge")
```

In each tool handler, log:
- Request received (tool name, truncated args)
- Before HA/InfluxDB call (URL, params)
- After HA/InfluxDB call (status code, response size in bytes)
- Before returning to MCP (response size)
- On exception (stack trace)

### Step 2: Run the bridge manually and inspect

```bash
cd ~/git/ha-mcp-bridge
source .venv/bin/activate
# Pipe a test JSON-RPC message to stdin, capture stdout
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ha_list_entities","arguments":{}}}' | ha-mcp-bridge
```

Test with increasingly large queries to find a size threshold.

### Step 3: Size-threshold test

Run these through the manual bridge, measure response size at each:

- `ha_state("sensor.plant_shelf_temperatures_tank_center")` — small, should work
- `ha_list_entities(domain="button")` — small, should work
- `ha_list_entities(domain="sensor")` — ~70KB, may fail
- `ha_list_entities(domain=None)` — all entities, hundreds of KB, likely fails
- `ha_query_grouped([5 plant shelf entities], hours=10)` — 359KB, known failure

Record at what size the failure starts.

### Step 4: If size-related — fix via truncation

Add to `ha_query_grouped` (and `ha_history_binned` if needed):

```python
MAX_RESPONSE_BYTES = 100_000  # or whatever threshold works

# After building the result:
import json
serialized = json.dumps(result)
if len(serialized) > MAX_RESPONSE_BYTES:
    return {
        "error": "response_too_large",
        "size_bytes": len(serialized),
        "hint": "Narrow the time window (hours=) or reduce entity_id count, or use ha_history_binned with larger bin_minutes instead."
    }
```

Similar guard for `influx_flux` based on row count.

### Step 5: Verify fix

Re-run the size-threshold tests. All calls should either succeed or return a clear `error` dict that Claude Code can surface to the user instead of hanging.

## Files to look at

- `/home/jay/git/ha-mcp-bridge/src/ha_mcp_bridge/server.py` — tool handlers
- `/home/jay/git/ha-mcp-bridge/src/ha_mcp_bridge/ha_client.py` — HA HTTP client
- `/home/jay/git/ha-mcp-bridge/src/ha_mcp_bridge/influx_client.py` — InfluxDB client + CSV parser
- `/home/jay/git/ha-mcp-bridge/.env` — has live `HA_TOKEN`, `INFLUX_TOKEN` (do not echo to logs)

## Reporting back

Write findings + fix summary to `/tmp/ha-mcp-bridge-debug-findings.md` before closing the parallel session. Main session will pick it up on next read.

## Testing protocol

Any fix needs to be tested against at least:
- 1 small query that worked before
- 1 known-large query that failed before
- 1 moderate query

Commit the fix with a conventional commit message (`fix(bridge): ...`) and push to `origin/master` on the private repo (`JayIsDed/ha-mcp-bridge`).

## Not in scope for this debug session

- Adding new MCP tools
- Refactoring existing code beyond the fix
- Rotating tokens (separate tidy-up item)
- Moving secrets to Infisical (separate tidy-up item)

Stay scoped to: identify hang cause, fix it, verify, commit, report.
