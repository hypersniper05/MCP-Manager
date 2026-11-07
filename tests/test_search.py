"""Search, and the call that follows it: the contract a hybrid group lives on.

Written against behaviour, not against embeddings. The model runs inside the manager, but the
suite runs without it (lexical) -- and what must not change between the two is what a caller gets
back: the same fields, the same 0..1 scale, the same floor, paging that behaves.

The second half is search as the model uses it: matches that carry their arguments, so
search -> call is two round trips and never three. What happens when the call itself is
wrong lives in test_coaching.py.
"""

import pytest
import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import API, ADMIN, MCP, BETA

pytestmark = pytest.mark.usefixtures("real_servers")

NATIVE = {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}
H = {"authorization": f"Bearer {ADMIN}"}
FIELDS = {"exposed", "mcp", "tool", "description", "source", "score", "arguments"}


def search(query, **kw):
    return httpx.post(f"{API}/search", headers=H, json={"query": query, **kw}).json()


def test_a_result_says_which_mcp_the_tool_is_on_and_what_it_does(manager):
    """Not a bare name: which server, which tool, what it does, whose words those are, its
    arguments, and how close the match is."""
    r = search("take a screenshot of the page", page_size=5)
    assert r["mode"] in ("semantic", "lexical") and r["results"]
    for hit in r["results"]:
        assert FIELDS <= set(hit)
        assert hit["mcp"] and hit["tool"] and hit["description"] and hit["arguments"]
        assert hit["source"] in ("mcp server", "edited here")
        assert 0.0 <= hit["score"] <= 1.0, "the score must be on the scale the floor means"


def test_scores_stay_on_one_scale_whichever_engine_answered(manager):
    """Raw BM25 happily returns 1.961, which is meaningless next to a cosine and breaks a
    floor written for one. Whichever engine answers, the numbers mean the same thing."""
    for q in ("browser tab", "generate an image", "download"):
        for hit in search(q, page_size=20)["results"]:
            assert 0.0 <= hit["score"] <= 1.0


def test_nothing_below_the_floor_comes_back(manager):
    hits = search("close", page_size=50)["results"]
    assert hits, "a word this common should match something"
    assert all(h["score"] >= 0.5 for h in hits)
    # and a query with no overlap at all is allowed to return nothing rather than noise
    assert search("qqzzxvwy nothinghere", page_size=10)["results"] == []


def test_a_singular_finds_the_plural(manager):
    """"tab" has to find list_tabs, in the lexical engine as much as the semantic one."""
    assert "list_tabs" in [h["exposed"] for h in search("list tab", page_size=10)["results"]]


def test_paging_is_stable_and_disjoint(manager):
    p1, p2 = search("browser", page_size=5), search("browser", page_size=5, page=2)
    assert p1["total"] == p2["total"] and p1["pages"] == p2["pages"]
    if p1["total"] > 5:
        assert len(p1["results"]) == 5 and p2["results"]
        assert not {h["exposed"] for h in p1["results"]} & {h["exposed"] for h in p2["results"]}
    # a page past the end clamps to the last page; it does not silently return nothing
    assert search("browser", page_size=5, page=999)["page"] == p1["pages"]


# ------------------------------------------------------------------ through MCP

async def _session(key, fn):
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=60) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


def _text(result):
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_search_then_call_is_two_round_trips(manager):
    """The search result carries the arguments, so the next call is the real one -- no
    'get schema' step between. And the client never re-lists: that is the whole point."""
    async def flow(s):
        found = _text(await s.call_tool("search_tools", {"query": "list the open browser tabs"}))
        assert "list_tabs [browser]" in found and "\n  args:" in found, found
        got = await s.call_tool("call_tool", {"name": "list_tabs", "arguments": {}})
        assert not got.is_error, _text(got)
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_searching_opens_nothing(manager):
    """Search shows what exists; only USING a server opens it into the list."""
    async def flow(s):
        before = {t.name for t in (await s.list_tools()).tools}
        await s.call_tool("search_tools", {"query": "list the open browser tabs"})
        assert {t.name for t in (await s.list_tools()).tools} == before
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_the_top_match_comes_with_a_call_to_copy(manager):
    async def flow(s):
        found = _text(await s.call_tool("search_tools", {"q": "read the text of the page"}))
        assert 'Run one with call_tool, e.g. call_tool {"name": ' in found, found
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_search_tools_pages_for_the_model(manager):
    async def flow(s):
        p1 = _text(await s.call_tool("search_tools", {"query": "browser", "page_size": 3}))
        if "page 1 of" not in p1:
            pytest.skip("not enough matches to page in this mode")
        assert "page=2" in p1
        p2 = _text(await s.call_tool("search_tools", {"query": "browser", "page_size": 3, "page": 2}))
        assert "page 2 of" in p2
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_an_empty_query_asks_for_everything_not_for_nothing(manager):
    """A model pages with an empty query -- it is asking 'and what else can I reach', not
    'match nothing'. Answering 'no matches' turns a reasonable question into a dead end."""
    async def flow(s):
        out = _text(await s.call_tool("search_tools", {"query": "", "page_size": 5}))
        assert "No tools match" not in out, out
        assert out.count("\n  args:") == 5, out
    await _session(BETA, flow)


@pytest.mark.asyncio
async def test_the_server_index_rides_in_the_search_tool_itself(manager):
    """Plenty of clients never show the model a server's instructions; every client shows
    it the tool descriptions. So search_tools says what is behind it."""
    counts = {}
    for t in httpx.get(f"{API}/tools", headers=H).json():
        counts[t["upstream"]] = counts.get(t["upstream"], 0) + 1

    async def flow(s):
        desc = next(t.description for t in (await s.list_tools()).tools if t.name == "search_tools")
        for name, count in counts.items():
            assert f"{name}: {count} tools" in desc, desc
    await _session(BETA, flow)
