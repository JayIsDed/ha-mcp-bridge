"""Shared fixtures."""

from __future__ import annotations

import pytest
from aiohttp import web
from stand_in import Handler


@pytest.fixture
async def stand_in_ha():
    """Start a stand-in HA with a given handler; returns its base URL."""
    runners: list[web.AppRunner] = []

    async def start(handler: Handler) -> str:
        app = web.Application()
        app.router.add_get("/api/states/{entity_id}", handler)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()
