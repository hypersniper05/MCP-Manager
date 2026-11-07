"""A group limited to chosen servers -- deterministic, no network, a lexical index.

A server left out of a group must not exist for its keys: not in a list, a search, the server
index, a suggestion, a failure's alternatives or the news, and no name of any spelling may reach
it. With no choice made, a group reaches every server, including one added later.
"""

import asyncio
import json
import threading

import mcp.types as types

from mcp_manager import db
from mcp_manager.gateway import Gateway, Link

TOOLS = {
    "browser": [("navigate", "Go to a URL and wait for it to load."),
                ("read_page", "Read the visible text of the page.")],
    "ytdl": [("download_audio", "Download a YouTube video's audio as an MP3."),
             ("read_transcript", "Read the text of a finished transcript.")],
}
NATIVE = {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}


def _gateway(mode="hybrid", servers=None, defaults="[]"):
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults, servers) VALUES ('g', ?, ?, ?)",
                     (mode, defaults, None if servers is None else json.dumps(servers)))
    database.execute("INSERT INTO keys (label, group_id, key_hash, created_at) VALUES ('app', 1, 'h', 'now')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    for i, (name, tools) in enumerate(TOOLS.items()):
        database.execute("INSERT INTO upstreams (name, url) VALUES (?, ?)", (name, f"http://127.0.0.1:{i + 1}/mcp"))
        link = Link(database.execute("SELECT * FROM upstreams WHERE name = ?", (name,)).fetchone())
        link.tools = [types.Tool(name=n, description=d, inputSchema={"type": "object"}) for n, d in tools]
        link.session = object()
        gw._links[name] = link
    gw.invalidate()
    return gw


def _listed(gw):
    return [t.name for t in asyncio.run(gw._list_tools(None, None)).tools]


def _call(gw, name, arguments=None):
    params = types.CallToolRequestParams(name=name, arguments=arguments or {})
    result, notes, *_ = asyncio.run(gw._dispatch(None, params, "c"))
    return result.is_error, "\n".join(c.text for c in result.content), notes


def _choose(gw, servers):
    gw.db.execute("UPDATE groups SET servers = ?", (None if servers is None else json.dumps(servers),))


# ---- automatic: nothing chosen, everything reached ------------------------------------------

def test_a_group_with_no_choice_reaches_every_server_even_a_new_one():
    gw = _gateway(mode="direct")
    assert set(_listed(gw)) == {t for tools in TOOLS.values() for t, _ in tools}
    gw.db.execute("INSERT INTO upstreams (name, url) VALUES ('new', 'http://127.0.0.1:3/mcp')")
    link = Link(gw.db.execute("SELECT * FROM upstreams WHERE name = 'new'").fetchone())
    link.tools = [types.Tool(name="fresh", description="A new tool.", inputSchema={"type": "object"})]
    gw._links["new"] = link
    gw.invalidate()
    assert "fresh" in _listed(gw)


# ---- chosen: the rest does not exist ---------------------------------------------------------

def test_a_direct_group_lists_only_its_servers():
    assert set(_listed(_gateway(mode="direct", servers=["ytdl"]))) == {"download_audio", "read_transcript"}


def test_a_new_server_stays_out_of_a_group_that_chose():
    gw = _gateway(mode="direct", servers=["ytdl"])
    gw.db.execute("INSERT INTO upstreams (name, url) VALUES ('new', 'http://127.0.0.1:3/mcp')")
    link = Link(gw.db.execute("SELECT * FROM upstreams WHERE name = 'new'").fetchone())
    link.tools = [types.Tool(name="fresh", description="A new tool.", inputSchema={"type": "object"})]
    gw._links["new"] = link
    gw.invalidate()
    assert "fresh" not in _listed(gw)


def test_no_spelling_of_a_tool_reaches_a_server_outside_the_group():
    gw = _gateway(servers=["ytdl"])
    for name in ("navigate", "Navigate", "browser__navigate", "browser/navigate", "browser.navigate"):
        failed, text, _ = _call(gw, "call_tool", {"name": name})
        assert failed and "no tool called" in text, (name, text)
    assert gw._find("read_pag", gw.group("g")) == (None, False), "a typo must not land on it either"


def test_a_group_with_no_servers_reaches_nothing_and_says_so():
    gw = _gateway(servers=[])
    assert set(_listed(gw)) == NATIVE
    assert _call(gw, "list_mcpservers")[1] == "{}"
    assert "The servers are: none." in _call(gw, "get_tools_from_server", {"server": "ytdl"})[1]


def test_search_finds_only_the_groups_tools():
    gw = _gateway(servers=["ytdl"])
    assert "navigate" not in _call(gw, "search_tools", {"query": ""})[1]
    assert "No tools" in _call(gw, "search_tools", {"query": "go to a url and load the page"})[1]
    text = _call(gw, "search_tools", {"query": "download audio"})[1]
    assert "download_audio" in text
    assert "browser" not in text


def test_the_settings_page_search_is_not_limited():
    gw = _gateway(servers=["ytdl"])
    names = {h["exposed"] for h in asyncio.run(gw.search(""))["results"]}
    assert {"navigate", "download_audio"} <= names


def test_the_server_index_and_the_servers_own_tools_show_only_the_group():
    gw = _gateway(servers=["ytdl"])
    tools = asyncio.run(gw._list_tools(None, None)).tools
    index = next(t.description for t in tools if t.name == "search_tools")
    assert "ytdl" in index and "browser" not in index
    assert "browser" not in gw.instructions()
    assert set(json.loads(_call(gw, "list_mcpservers")[1])) == {"ytdl"}
    failed, text, _ = _call(gw, "get_tools_from_server", {"server": "browser"})
    assert failed and text == 'There is no server called "browser". The servers are: ytdl.'


def test_a_wrong_name_is_not_answered_with_a_tool_outside_the_group():
    gw = _gateway(servers=["ytdl"])
    text = _call(gw, "call_tool", {"name": "read_pages"})[1]
    assert "read_page" not in text.replace("read_pages", "")


def test_a_failure_offers_no_alternative_outside_the_group():
    gw = _gateway(servers=["ytdl"])
    assert asyncio.run(gw._alternatives(("ytdl", "read_transcript"), "read_transcript", gw.group("g"))) == []
    _choose(gw, None)
    found = asyncio.run(gw._alternatives(("ytdl", "read_transcript"), "read_transcript", gw.group("g")))
    assert found and "read_page" in found[0], "with both servers reached, the browser's reader is the alternative"


def test_news_is_only_about_the_groups_servers():
    gw = _gateway(servers=["ytdl"])
    group = gw.group("g")
    assert gw._news("c", group) == []
    gw._links["browser"].session = None          # down, but not this group's business
    assert gw._news("c", group) == []
    gw._links["ytdl"].session = None
    assert any("ytdl just went down" in n for n in gw._news("c", group))


def test_adding_and_removing_a_server_reads_as_news():
    gw = _gateway(servers=["ytdl"])
    gw._news("c", gw.group("g"))
    _choose(gw, ["browser", "ytdl"])
    assert any(n.startswith("New server: browser") for n in gw._news("c", gw.group("g")))
    _choose(gw, ["ytdl"])
    assert "browser was removed." in gw._news("c", gw.group("g"))


# ---- defaults and learned tools ---------------------------------------------------------------

def test_a_default_on_a_server_left_out_is_kept_but_not_listed_and_is_flagged():
    gw = _gateway(servers=["ytdl"], defaults='["navigate", "download_audio"]')
    assert set(_listed(gw)) == NATIVE | {"download_audio"}
    assert gw.unresolved(gw.group("g")) == ["navigate"]
    _choose(gw, None)
    assert "navigate" in _listed(gw) and gw.unresolved(gw.group("g")) == []


def test_a_learned_tool_leaves_with_its_server_and_comes_back_with_it():
    gw = _gateway(mode="adaptive")
    from mcp_manager import gate
    k, g = gate.current_key.set(1), gate.current_group.set("g")
    try:
        for _ in range(3):
            gw.adaptive.observe(1, ("browser", "navigate"), gw._no_slot(gw.group("g")))
        assert "navigate" in _listed(gw)
        _choose(gw, ["ytdl"])
        gw.recap()
        assert "navigate" not in _listed(gw)
        assert gw.adaptive_view(1, gw.group("g"))["tools"] == [], "the ranking shows what it can reach"
        _choose(gw, None)
        gw.recap()
        assert "navigate" in _listed(gw), "its habits were never forgotten"
    finally:
        gate.current_key.reset(k)
        gate.current_group.reset(g)


# ---- sessions ---------------------------------------------------------------------------------

class _Session:
    scope = None

    def __init__(self):
        self.stop = threading.Event()


def test_only_sessions_out_of_reach_close():
    gw = _gateway(servers=["ytdl"])
    out, kept, shared = _Session(), _Session(), _Session()
    gw._clients = {(1, "browser"): out, (1, "ytdl"): kept, (("group", 1), "browser"): shared}
    gw.close_unreachable()
    assert out.stop.is_set() and shared.stop.is_set() and not kept.stop.is_set()
    assert set(gw._clients) == {(1, "ytdl")}


def test_a_group_on_automatic_keeps_every_session():
    gw = _gateway()
    session = _Session()
    gw._clients = {(1, "browser"): session}
    gw.close_unreachable()
    assert not session.stop.is_set() and (1, "browser") in gw._clients


# ---- names change ------------------------------------------------------------------------------

def test_a_renamed_server_stays_chosen():
    gw = _gateway(servers=["browser", "ytdl"])
    gw.rename_upstream("browser", "Tor Browser")
    assert json.loads(gw.group("g")["servers"]) == ["Tor Browser", "ytdl"]


def test_a_removed_server_leaves_the_groups_that_chose_it():
    gw = _gateway(servers=["browser", "ytdl"])
    db.server_moved(gw.db, "browser", None)
    assert json.loads(gw.group("g")["servers"]) == ["ytdl"]


def test_a_group_on_automatic_is_left_alone_by_a_rename():
    gw = _gateway()
    gw.rename_upstream("browser", "Tor Browser")
    assert gw.group("g")["servers"] is None


def test_the_tool_list_says_what_an_edit_would_revert_to():
    gw = _gateway()
    db.set_override(gw.db, "browser", "navigate", "My words.")
    tool = next(t for t in gw.all_tools() if t["tool"] == "navigate")
    assert tool["overridden"] and tool["description"] == "My words."
    assert tool["server_says"] == "Go to a URL and wait for it to load."
