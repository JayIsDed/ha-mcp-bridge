"""Tests for ha_camera_snapshot and ha_logbook.

Camera tests generate a synthetic JPEG in-process and monkeypatch the client so
we don't need a real camera. Logbook tests monkeypatch get_logbook likewise.
"""

from __future__ import annotations

import importlib
from io import BytesIO

import pytest
from PIL import Image as PILImage


@pytest.fixture
def server_mod(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-for-import")
    import ha_mcp_bridge.server as mod

    importlib.reload(mod)
    yield mod
    importlib.reload(mod)


def _make_jpeg(width: int, height: int, color: tuple[int, int, int] = (40, 160, 60)) -> bytes:
    """Synthesize a JPEG of the given dimensions."""
    img = PILImage.new("RGB", (width, height), color=color)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class _FakeHA:
    def __init__(self, *, snapshot: bytes | None = None, logbook: list | None = None, raise_on: str | None = None):
        self._snapshot = snapshot
        self._logbook = logbook
        self._raise_on = raise_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get_camera_snapshot(self, entity_id):
        if self._raise_on == "snapshot":
            from ha_mcp_bridge.ha_client import HAError
            raise HAError("simulated camera error")
        return self._snapshot

    async def get_logbook(self, hours, entity_id=None):
        if self._raise_on == "logbook":
            from ha_mcp_bridge.ha_client import HAError
            raise HAError("simulated logbook error")
        return self._logbook or []


# ---- camera tests ----


async def test_camera_wrong_domain_rejected(server_mod) -> None:
    out = await server_mod.ha_camera_snapshot("sensor.not_a_camera")
    assert out["error"] == "invalid_entity_domain"


async def test_camera_returns_image_object(server_mod, monkeypatch) -> None:
    raw = _make_jpeg(3840, 2160)  # 4K frame
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(snapshot=raw))

    out = await server_mod.ha_camera_snapshot("camera.reolink_e1", max_width=1024, quality=70)
    assert isinstance(out, server_mod.Image)
    # Verify the image was actually resized — decode the returned bytes.
    decoded = PILImage.open(BytesIO(out.data))
    assert decoded.width == 1024
    assert decoded.height == int(1024 * 2160 / 3840)  # aspect preserved


async def test_camera_no_resize_when_smaller(server_mod, monkeypatch) -> None:
    raw = _make_jpeg(640, 480)
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(snapshot=raw))

    out = await server_mod.ha_camera_snapshot("camera.small", max_width=1024)
    assert isinstance(out, server_mod.Image)
    decoded = PILImage.open(BytesIO(out.data))
    assert decoded.width == 640  # unchanged


async def test_camera_ha_error_returns_dict(server_mod, monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(raise_on="snapshot"))
    out = await server_mod.ha_camera_snapshot("camera.broken")
    assert "error" in out
    assert "simulated camera error" in out["error"]


async def test_camera_bad_bytes_returns_error(server_mod, monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(snapshot=b"not an image"))
    out = await server_mod.ha_camera_snapshot("camera.weird")
    assert "error" in out
    assert "image_processing_failed" in out["error"]


# ---- logbook tests ----


async def test_logbook_returns_entries(server_mod, monkeypatch) -> None:
    fake = [
        {"when": "2026-04-17T18:00:00Z", "name": "Heater", "message": "turned on", "entity_id": "switch.heater"},
        {"when": "2026-04-17T18:15:00Z", "name": "Heater", "message": "turned off", "entity_id": "switch.heater"},
    ]
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(logbook=fake))
    out = await server_mod.ha_logbook(hours=1)
    assert out == fake


async def test_logbook_clamps_hours(server_mod, monkeypatch) -> None:
    """Negative/huge hours should be clamped."""
    captured: dict = {}

    class _Capture(_FakeHA):
        async def get_logbook(self, hours, entity_id=None):
            captured["hours"] = hours
            return []

    monkeypatch.setattr(server_mod, "_client", lambda: _Capture())
    await server_mod.ha_logbook(hours=-5)
    assert captured["hours"] == 1  # clamped low bound
    await server_mod.ha_logbook(hours=10000)
    assert captured["hours"] == server_mod.HA_HISTORY_MAX_HOURS


async def test_logbook_ha_error_returns_error_list(server_mod, monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(raise_on="logbook"))
    out = await server_mod.ha_logbook(hours=1)
    assert len(out) == 1
    assert "error" in out[0]


async def test_logbook_size_guard_trips(server_mod, monkeypatch) -> None:
    """Huge logbook payload should route through the size guard."""
    monkeypatch.setattr(server_mod, "MAX_RESPONSE_BYTES", 500)
    big = [{"when": "2026-04-17T18:00:00Z", "name": "X" * 50, "message": "y" * 50} for _ in range(20)]
    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA(logbook=big))

    out = await server_mod.ha_logbook(hours=1)
    assert len(out) == 1
    assert out[0]["error"] == "response_too_large"
    assert out[0]["tool"] == "ha_logbook"
