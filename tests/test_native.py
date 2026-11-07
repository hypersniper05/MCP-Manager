"""The manager's own tools in a hybrid group -- deterministic, no network, a lexical index.

list_mcpservers, get_tools_from_server and search_tools are what a model reads instead of a tool
list, so they are held to two things at once: enough to make the right call first time, and as
few tokens as that takes.
"""

import asyncio
import json

import mcp.types as types

from mcp_manager import db, scoring
from mcp_manager.gateway import Gateway, Link

SERVERS = {
    "secure_browser": (None, [
        ("screenshot", "Screenshot the viewport or the full page. "
                       + "Image px = viewport px; `grid` overlays labelled coordinates to click at. " * 3,
         {"properties": {"fullPage": {"type": "boolean", "description": "the whole page, not just the viewport"},
                         "tab": {"type": "string", "description": "which tab (default: the active one)"}}}),
        ("navigate", "Go to a URL and wait for it to load.",
         {"properties": {"url": {"type": "string", "description": "the address to open"},
                         "tab": {"type": "string", "description": "which tab"}}, "required": ["url"]}),
        ("read_text", "Read the visible text of the page.", {}),
    ]),
    "ytdl": ("YouTube downloader: MP3/MP4 downloads and Whisper transcripts of YouTube videos.", [
        ("download_audio", "Download a YouTube video's audio as an MP3. Can also transcribe it.",
         {"properties": {"url": {"type": "string", "description": "the YouTube video URL"},
                         "bitrate": {"type": "integer", "default": 320, "description": "kbps"}},
          "required": ["url"]}),
        ("read_transcript", "Read the text of a finished transcript.",
         {"properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}),
    ]),
}


def _gateway(up=("secure_browser", "ytdl")):
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('g', 'hybrid', '[]')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    for i, (name, (desc, tools)) in enumerate(SERVERS.items()):
        database.execute("INSERT INTO upstreams (name, url, description) VALUES (?, ?, ?)",
                         (name, f"http://127.0.0.1:{i + 1}/mcp", desc))
        link = Link(database.execute("SELECT * FROM upstreams WHERE name = ?", (name,)).fetchone())
        link.tools = [types.Tool(name=n, description=d, inputSchema={"type": "object", **s}) for n, d, s in tools]
        link.instructions = f"The {name} server's own words."
        link.session = object() if name in up else None
        gw._links[name] = link
    gw.invalidate()
    return gw


def _call(gw, name, arguments=None, conv="c"):
    params = types.CallToolRequestParams(name=name, arguments=arguments or {})
    result, notes, *_ = asyncio.run(gw._dispatch(None, params, conv))
    return result.is_error, "\n".join(c.text for c in result.content), notes


# ---- word search on a small catalogue -------------------------------------------------

def test_one_server_alone_is_found_by_its_own_words():
    """A word in every tool scored 0: a new install's first server found nothing by word search."""
    docs = {f"t{i}": scoring.document(f"tool_{i}", f"Reads note {i} from the shelf.", None, "spike", "Notes server")
            for i in range(3)}
    index = scoring.build(docs, {})
    assert index["mode"] == "lexical"
    for word in ("note", "shelf", "spike"):
        assert len(scoring.top_k(index, word)) == 3, word


def test_a_single_tool_is_found_too():
    index = scoring.build({"only": scoring.document("only", "Reads a note.", None, "spike", None)}, {})
    assert [name for name, _ in scoring.top_k(index, "note")] == ["only"]


def test_the_rarer_word_still_decides():
    docs = {"a": scoring.document("a", "Reads a note from the shelf.", None, "spike", None),
            "b": scoring.document("b", "Writes a note in the margin.", None, "spike", None)}
    assert scoring.top_k(scoring.build(docs, {}), "note margin")[0][0] == "b"


# ---- list_mcpservers --------------------------------------------------------------

def test_list_mcpservers_gives_each_server_its_description_tool_count_and_state():
    gw = _gateway(up=("secure_browser",))
    err, text, _ = _call(gw, "list_mcpservers")
    servers = json.loads(text)
    assert not err and list(servers) == ["secure_browser", "ytdl"]
    assert servers["ytdl"] == {"description": SERVERS["ytdl"][0], "tools": 2, "status": "down"}
    # no admin description: the server's own words stand in
    assert servers["secure_browser"] == {"description": "The secure_browser server's own words.",
                                         "tools": 3, "status": "up"}


def test_an_admin_edit_is_what_list_mcpservers_says():
    gw = _gateway()
    gw.db.execute("UPDATE upstreams SET description = 'Drives a real browser.' WHERE name = 'secure_browser'")
    assert json.loads(_call(gw, "list_mcpservers")[1])["secure_browser"]["description"] == "Drives a real browser."


def test_the_names_models_reach_for_find_it():
    gw = _gateway()
    for name in ("list_servers", "listMcpServers", "list-mcp-servers"):
        err, text, _ = _call(gw, name)
        assert not err and '"ytdl"' in text, (name, text)
    err, text, _ = _call(gw, "call_tool", {"name": "list_mcpservers"})       # and through the door
    assert not err and '"ytdl"' in text


# ---- get_tools_from_server -----------------------------------------------------------

def test_get_tools_from_server_is_a_dict_of_what_each_tool_does_and_takes():
    gw = _gateway()
    err, text, _ = _call(gw, "get_tools_from_server", {"server": "ytdl"})
    out = json.loads(text)
    assert not err and out["server"] == "ytdl" and out["status"] == "up"
    assert out["tools"] == {
        "download_audio": "Download a YouTube video's audio as an MP3. Can also transcribe it. (args: url, bitrate?)",
        "read_transcript": "Read the text of a finished transcript. (args: task_id)"}


def test_a_server_is_found_by_the_part_of_its_name_a_model_uses():
    gw = _gateway()
    err, text, _ = _call(gw, "get_tools_from_server", {"server": "Browser"})
    assert not err and json.loads(text)["server"] == "secure_browser", text


def test_an_unknown_server_is_answered_with_the_ones_there_are():
    gw = _gateway()
    err, text, _ = _call(gw, "get_tools_from_server", {"server": "photoshop"})
    assert err and 'no server called "photoshop"' in text and "secure_browser, ytdl" in text, text
    err, text, _ = _call(gw, "get_tools_from_server", {})
    assert err and '{"server": "<name>"}' in text and "secure_browser, ytdl" in text, text


def test_a_server_just_listed_is_not_listed_again_on_its_first_use():
    """get_tools_from_server, then a call there: the first-use note used to repeat every tool."""
    gw = _gateway()

    async def ran(link, key, args, exposed, notes):
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")]), True
    gw._send = ran
    _call(gw, "get_tools_from_server", {"server": "secure_browser"})
    _, _, notes = _call(gw, "call_tool", {"name": "read_text"})
    joined = "\n".join(notes)
    assert "which you listed with get_tools_from_server" in joined and "navigate(" not in joined, joined
    _, _, notes = _call(gw, "call_tool", {"name": "download_audio", "arguments": {"url": "x"}}, conv="c")
    assert "You just used ytdl" in "\n".join(notes), "a server it did not list still gets the full list"


# ---- search_tools -------------------------------------------------------------------

def test_search_on_one_server_ranks_within_that_server():
    """The floors are relative to the best match. Ranked across all servers, a query whose best
    match is elsewhere would cut every match on the server that was asked for."""
    gw = _gateway()
    err, text, _ = _call(gw, "search_tools", {"query": "read the text", "server": "ytdl"})
    assert not err and "read_transcript [ytdl]" in text and "read_text" not in text, text
    assert 'matches for "read the text" on ytdl' in text


def test_a_search_match_is_what_it_takes_to_call_it_and_no_more():
    gw = _gateway()
    text = _call(gw, "search_tools", {"query": "screenshot of the page"})[1]
    assert "screenshot [secure_browser] Screenshot the viewport or the full page." in text, text
    assert text.count("Image px") < 3, "only the opening of the description: whole sentences up to ~200 chars"
    assert "similarity" not in text, "the order already says which fits best"
    assert "fullPage?: boolean;" in text and "the whole page" not in text, "an optional argument: its type only"
    text = _call(gw, "search_tools", {"query": "go to a url"})[1]
    assert "url: string (the address to open)" in text, "a required one says what it takes"


def test_a_down_server_is_said_once_not_on_every_match():
    gw = _gateway(up=("secure_browser",))
    text = _call(gw, "search_tools", {"query": "", "server": "ytdl"})[1]
    assert text.count("ytdl is down right now") == 1 and "[ytdl, down]" in text, text


def test_the_index_in_search_tools_is_brief_and_list_mcpservers_has_the_rest():
    gw = _gateway()
    gw.db.execute("UPDATE upstreams SET description = ? WHERE name = 'ytdl'",
                  ("Downloads YouTube videos as MP3 or MP4. "
                   + "Transcribes them with Whisper, and separates vocals from the background. " * 3,))
    tools = asyncio.run(gw._list_tools(None, None)).tools
    assert {t.name for t in tools} == {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}
    desc = next(t.description for t in tools if t.name == "search_tools")
    assert "ytdl: 2 tools - Downloads YouTube videos as MP3 or MP4." in desc and "Whisper" not in desc, desc
    assert "Whisper" in json.loads(_call(gw, "list_mcpservers")[1])["ytdl"]["description"]
