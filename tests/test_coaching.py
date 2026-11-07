"""Every call result is a message the manager can put something useful in. These pin down
the ones that save a model a round trip, against the live upstreams.

Only calls with no side effects: list_tabs, read_text, read_download on an id that does not
exist. The browser is a real, shared one.
"""

import asyncio

import httpx2 as httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import ALPHA, BETA, MCP

pytestmark = pytest.mark.usefixtures("real_servers")

NOTE = "[mcp-manager]"


async def _session(key, fn, on_message=None):
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=60) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w, message_handler=on_message) as s:
                await s.initialize()
                return await fn(s)


def _text(result):
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


# ---- the name ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_name_spelt_differently_runs_and_says_the_right_spelling(manager):
    """listTabs can only mean list_tabs. Refusing it would cost a round trip to learn nothing."""
    async def flow(s):
        for name in ("listTabs", "List-Tab", "browser.ListTabs", "browser/list_tabs"):
            got = await s.call_tool("call_tool", {"name": name})
            assert not got.is_error, f"{name}: {_text(got)}"
        got = _text(await s.call_tool("call_tool", {"name": "listTabs"}))
        assert 'There is no tool "listTabs"; I ran list_tabs' in got, got
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_a_typo_runs_for_a_read_and_is_refused_for_a_write(manager):
    """A typo is a guess. For a read the guess is harmless, so it runs with the right
    spelling noted; for a write it is refused with "did you mean" and the call to make --
    refusing costs one round trip, guessing wrong costs a side effect."""
    async def flow(s):
        got = await s.call_tool("call_tool", {"name": "screnshot"})   # screenshot is readOnlyHint
        text = _text(got)
        assert not got.is_error and 'There is no tool "screnshot"; I ran screenshot' in text, text
        # download_video is not readOnlyHint and not in this group's defaults, so the advice
        # must name the call this client can actually make: through call_tool
        got = await s.call_tool("call_tool", {"name": "download_vidoe"})
        text = _text(got)
        assert got.is_error and "Did you mean download_video (ytdl)?" in text, text
        assert "How to call download_video:" in text, text
        assert 'call_tool {"name": "download_video"' in text, text
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_a_name_two_servers_share_asks_which_one(manager):
    async def flow(s):
        text = _text(await s.call_tool("call_tool", {"name": "list_downloads"}))
        assert "More than one server" in text
        assert "browser__list_downloads" in text and "ytdl__list_downloads" in text
    await _session(BETA, flow)


# ---- the arguments -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_argument_slips_are_fixed_and_said(manager):
    """A key in the wrong case and a number sent as a string: one reading each, so the call
    runs, and the note teaches the right form."""
    async def flow(s):
        got = await s.call_tool("call_tool", {"name": "read_text", "arguments": {"maxlength": "1000"}})
        note = _text(got)
        assert not got.is_error, note
        assert NOTE in note and '"maxlength" is spelled "maxLength"' in note and "became 1000" in note
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_a_near_miss_key_for_a_missing_required_argument_is_renamed(manager):
    async def flow(s):
        text = _text(await s.call_tool("call_tool", {"name": "read_download", "arguments": {"idd": "nope"}}))
        assert '"idd" is "id"' in text, text
        assert "missing required" not in text, "it reached the upstream with the right key"
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_a_missing_required_argument_is_refused_with_the_usage(manager):
    """The upstream would refuse it too -- less helpfully, and a hop later."""
    async def flow(s):
        got = await s.call_tool("call_tool", {"name": "navigate", "arguments": {}})
        text = _text(got)
        assert got.is_error and "navigate did not run" in text
        assert 'missing required argument "url"' in text and "How to call navigate:" in text, text
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_call_tool_reads_the_shapes_models_actually_send(manager):
    """tool/args instead of name/arguments, arguments as a string, arguments flattened next
    to the name: each has one reading, so each runs, and the note shows the proper shape."""
    async def flow(s):
        got = await s.call_tool("call_tool", {"tool": "list_tabs", "args": {}})
        assert not got.is_error and "I read your call that way" in _text(got)
        assert not (await s.call_tool("call_tool", {"name": "list_tabs", "arguments": "{}"})).is_error
        moved = _text(await s.call_tool("call_tool", {"name": "read_download", "id": "nope"}))
        assert "I moved id there" in moved, moved
        bad = await s.call_tool("call_tool", {"name": "list_tabs", "arguments": "not json"})
        assert bad.is_error and "JSON object" in _text(bad)
    await _session(BETA, flow)


# ---- first use of a server -----------------------------------------------------------

@pytest.mark.asyncio
async def test_first_use_of_a_server_lists_all_its_tools_once(manager):
    """The user's rule: use one tool, get the whole server. The result says so and lists them,
    so a client that never re-lists still knows everything without a search."""
    async def flow(s):
        first = _text(await s.call_tool("call_tool", {"name": "list_tabs"}))
        note = first[first.index("[mcp-manager]"):]
        assert "You just used browser" in note and "Also on browser:" in note, note
        assert "screenshot" in note and "navigate" in note, "every tool is named, in full or by name"
        # a big server: the ten closest in full, the rest by name -- not 1,800 tokens of everything
        assert note.count("\n- ") == 10 and len(note) < 3500, len(note)
        again = _text(await s.call_tool("call_tool", {"name": "list_tabs"}))
        assert "You just used" not in again, "said once per conversation, not on every call"
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_a_client_that_listens_gets_the_server_in_its_list(manager):
    seen = []

    async def on_message(msg):
        seen.append(getattr(msg, "method", None))

    async def flow(s):
        init_list = {t.name for t in (await s.list_tools()).tools}
        assert "screenshot" not in init_list
        await s.call_tool("call_tool", {"name": "list_tabs"})
        await asyncio.sleep(1)
        assert "notifications/tools/list_changed" in seen
        assert "screenshot" in {t.name for t in (await s.list_tools()).tools}
    await _session(BETA, flow, on_message)


@pytest.mark.asyncio
async def test_after_a_relist_the_advice_is_a_direct_call(manager):
    """Once the client has re-listed, a server it opened is in its own tool list, so the advice
    is the direct call. (The other half -- before a re-list, call_tool -- is tested against the
    gateway itself in test_conversations.py: the Python client re-lists on list_changed at its own
    pace, which makes that half a race through a real client.)"""
    async def flow(s):
        await s.list_tools()
        await s.call_tool("call_tool", {"name": "list_tabs"})                  # opens browser
        await s.list_tools()                                                   # it re-lists
        after = _text(await s.call_tool("navigate", {}))
        assert 'e.g. navigate {"url": "<string>"}' in after, after
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_direct_mode_has_nothing_to_open(manager):
    async def flow(s):
        text = _text(await s.call_tool("list_tabs", {}))
        assert NOTE not in text, "a direct client already has every tool"
    await _session(ALPHA, flow)
