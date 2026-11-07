"""The answer to a wrong call, deterministic -- no network, a lexical index.

Each one is what the model reads instead of the result it wanted, so it has to be both right
and enough to make the next call the correct one.
"""

import asyncio

import mcp.types as types

from mcp_manager import db
from mcp_manager.gateway import Gateway, Link

TOOLS = {
    "browser": [("screenshot", "Take a screenshot of the page.", {}),
                ("read_text", "Read the visible text of the page.", {})],
    "ytdl": [("get_transcript", "Return the transcript text of a completed job.",
              {"properties": {"task_id": {"type": "string"}, "max_chars": {"type": "integer"}},
               "required": ["task_id"], "additionalProperties": False}),
             ("transcribe_video", "Transcribe a YouTube video to text with Whisper.",
              {"properties": {"url": {"type": "string"}}, "required": ["url"]})],
}


def _gateway():
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('g', 'hybrid', '[]')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    for i, (up, tools) in enumerate(TOOLS.items()):
        database.execute("INSERT INTO upstreams (name, url) VALUES (?, ?)", (up, f"http://127.0.0.1:{i + 1}/mcp"))
        link = Link(database.execute("SELECT * FROM upstreams WHERE name = ?", (up,)).fetchone())
        link.tools = [types.Tool(name=n, description=d, inputSchema={"type": "object", **s}) for n, d, s in tools]
        gw._links[up] = link
    gw.invalidate()
    return gw, gw.group("g")


def _call(gw, name, arguments):
    params = types.CallToolRequestParams(name="call_tool", arguments={"name": name, "arguments": arguments})
    result, *_ = asyncio.run(gw._dispatch(None, params, "c"))
    return result.is_error, "\n".join(c.text for c in result.content)


def test_a_misspelling_is_did_you_mean():
    gw, group = _gateway()
    text = asyncio.run(gw._did_you_mean("get_transcrpt", group, "c"))
    assert "Did you mean get_transcript (ytdl)?" in text and "How to call get_transcript" in text, text


def test_a_name_only_loosely_like_a_tool_is_not_offered_as_the_one_meant():
    """translate_text spells like get_transcript and nothing here translates. "Did you mean
    get_transcript", with its usage, invited a call that could never do the job."""
    gw, group = _gateway()
    text = asyncio.run(gw._did_you_mean("translate_text", group, "c"))
    assert "Did you mean" not in text and "How to call" not in text, text
    assert "No tool has a name like it" in text and "check that one really does the job" in text, text
    assert "get_transcript(task_id, max_chars?) (ytdl)" in text, "still listed, with how to call it"


def test_a_refused_call_names_every_correction_not_only_the_blocking_one():
    """The model resends the whole call after a refusal. Told only that task_id is missing, it
    sends "format" again -- and learns about that one a round later."""
    gw, _ = _gateway()
    err, text = _call(gw, "get_transcript", {"format": "text"})
    assert err and 'missing required argument "task_id"' in text, text
    assert '- also: dropped "format", which is not one of its arguments' in text, text
