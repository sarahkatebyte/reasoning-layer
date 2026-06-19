"""
Smoke tests for the MCP server / proxy.
Requires the proxy to be running on localhost:8000.
Run with: pytest tests/ -m smoke
"""

import asyncio
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_server import proxy_health, route, chat

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_proxy_health():
    result = await proxy_health()
    assert "ok" in result.lower() or "true" in result.lower()


@pytest.mark.asyncio
async def test_route():
    result = await route("explain the tradeoffs between postgres and elasticsearch for semantic search")
    assert result and len(result) > 0


@pytest.mark.asyncio
async def test_chat():
    result = await chat("What am I currently working on?", call_site="pytest-smoke")
    assert result and len(result) > 0
