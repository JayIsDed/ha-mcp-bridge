from __future__ import annotations

import csv
import io
from typing import Any

import aiohttp


class InfluxError(Exception):
    """Raised when InfluxDB returns an error or is unreachable."""


class InfluxClient:
    """Thin async client for InfluxDB 2.x flux queries.

    Read-only by design — writes go through HA's native integration, not this bridge.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        org: str,
        timeout: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._org = org
        self._headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "InfluxClient":
        self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def query(self, flux: str) -> list[dict[str, Any]]:
        """Execute a Flux query, return rows as list of dicts.

        InfluxDB returns annotated CSV — we parse it into records with typed values.
        Each row is keyed by column name; all values arrive as strings from CSV and
        numeric fields stay as strings unless InfluxDB's datatype row marks them —
        we coerce known _value + _time + duration types.
        """
        assert self._session is not None, "InfluxClient must be used as async context manager"
        url = f"{self._base}/api/v2/query"
        params = {"org": self._org}
        try:
            async with self._session.post(url, params=params, data=flux.encode("utf-8")) as resp:
                if resp.status == 401:
                    raise InfluxError("Unauthorized — check INFLUX_TOKEN.")
                if resp.status >= 400:
                    text = await resp.text()
                    raise InfluxError(f"InfluxDB {resp.status}: {text[:500]}")
                body = await resp.text()
        except aiohttp.ClientConnectorError as e:
            raise InfluxError(f"Cannot reach InfluxDB at {self._base}: {e}") from e
        except aiohttp.ClientError as e:
            raise InfluxError(f"HTTP error talking to InfluxDB: {e}") from e

        return _parse_csv(body)


def _parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse InfluxDB Flux CSV into list of dict rows.

    Handles both modes:
      - Plain CSV: first row is column names, data rows follow.
      - Annotated CSV: #datatype / #group / #default rows precede the header.

    Tables are separated by blank lines in the annotated mode. We flatten all tables
    into one list of records, preserving table index as `_table`.

    Numeric coercion happens when datatype annotations are present; otherwise we
    best-effort numeric-coerce values that parse cleanly as int/float.
    """
    reader = csv.reader(io.StringIO(text))
    records: list[dict[str, Any]] = []
    datatypes: list[str] = []
    columns: list[str] = []
    table_index = 0

    for raw_row in reader:
        if not raw_row:
            # Blank line — new table boundary; reset header state.
            datatypes = []
            columns = []
            continue

        head = raw_row[0]
        if head == "#datatype":
            datatypes = raw_row[1:]
            columns = []
            continue
        if head in ("#group", "#default"):
            continue
        if not columns:
            columns = raw_row[1:]
            continue

        values = raw_row[1:]
        record: dict[str, Any] = {"_table": table_index}
        for i, col in enumerate(columns):
            if col == "":
                continue
            val = values[i] if i < len(values) else ""
            dtype = datatypes[i] if i < len(datatypes) else ""
            record[col] = _coerce(val, dtype) if dtype else _best_effort_coerce(val)
        records.append(record)

        # Track table boundary via the `table` column if present (Flux emits per-table ints).
        try:
            idx_table = columns.index("table")
            raw_table = values[idx_table] if idx_table < len(values) else ""
            if raw_table.isdigit():
                table_index = int(raw_table)
        except ValueError:
            pass

    return records


def _best_effort_coerce(value: str) -> Any:
    if value == "":
        return None
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        if "." in value or "e" in value or "E" in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _coerce(value: str, dtype: str) -> Any:
    if value == "":
        return None
    if dtype in ("long", "int"):
        try:
            return int(value)
        except ValueError:
            return value
    if dtype in ("double", "float"):
        try:
            return float(value)
        except ValueError:
            return value
    if dtype == "boolean":
        return value.lower() in ("true", "t", "1")
    return value
