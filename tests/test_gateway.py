"""The flagship feature, proven end to end against three live upstreams.

Same pool. Two keys, two modes, two different views. And real calls through the gateway.
If these pass, the product works; everything else is polish.
"""

from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import ALPHA, BETA, MCP

pytestmark = pytest.mark.usefixtures("real_servers")

NATIVE = {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}   # a hybrid group's own tools


@asynccontextmanager
async def client(key):
    transport = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)
    async with transport, streamable_http_client(MCP, http_client=transport) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def names(s):
    return {t.name for t in (await s.list_tools()).tools}


@pytest.mark.asyncio
async def test_direct_lists_every_tool_and_nothing_to_search_with(manager):
    """direct: the whole pool is in the list, so search and call_tool would only be noise."""
    async with client(ALPHA) as a:
        seen = await names(a)
        assert len(seen) > 50, f"alpha should see the full pool, saw {len(seen)}"
        assert not seen & NATIVE, "a direct group has nothing to search for"


@pytest.mark.asyncio
async def test_hybrid_lists_its_defaults_and_the_managers_own_tools(manager):
    async with client(BETA) as b:
        assert await names(b) == {"server_status", "generate_image", "list_images"} | NATIVE


@pytest.mark.asyncio
async def test_two_keys_two_views_one_pool(manager):
    async with client(ALPHA) as a, client(BETA) as b:
        big, small = await names(a), await names(b)
        assert (small - NATIVE) < big


@pytest.mark.asyncio
async def test_call_passes_through(manager):
    """A real tool, called through the gateway, answered by the real upstream."""
    async with client(ALPHA) as a:
        result = await a.call_tool("server_status", {})
        assert not result.is_error
        assert "ready" in result.content[0].text.lower() or "model" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_colliding_names_route_to_the_right_upstream(manager):
    """browser and ytdl BOTH define list_downloads. Namespacing must survive listing
    AND routing — a rename that only fixes one side silently drops the tool."""
    async with client(ALPHA) as a:
        seen = await names(a)
        assert "browser__list_downloads" in seen and "ytdl__list_downloads" in seen
        for tool in ("browser__list_downloads", "ytdl__list_downloads"):
            result = await a.call_tool(tool, {})
            assert not result.is_error, f"{tool} failed: {result.content[0].text}"


@pytest.mark.asyncio
async def test_a_hybrid_client_that_emits_an_unlisted_name_is_served(manager):
    """Some clients pass any name the model writes straight through. A hybrid group reaches
    every tool through call_tool anyway, so refusing the direct spelling would only cost a
    round trip."""
    async with client(BETA) as b:
        result = await b.call_tool("list_tabs", {})
        assert not result.is_error, result.content[0].text


@pytest.mark.asyncio
async def test_an_unknown_tool_is_named_in_the_error(manager):
    async with client(ALPHA) as a:
        result = await a.call_tool("no_such_tool_anywhere", {})
        assert result.is_error and "no_such_tool_anywhere" in result.content[0].text
