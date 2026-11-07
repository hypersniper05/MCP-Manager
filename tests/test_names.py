"""Tool names when two servers share one -- deterministic, no network.

A clash renames both tools to say whose each is. The new names have to be names every client
accepts, the old spellings must still find the tool, a group's defaults must follow the rename
instead of breaking, and every change must be written down in words for the audit.
"""

import json
import re

import mcp.types as types

from mcp_manager import db
from mcp_manager.gateway import Gateway, Link

IMG = ("server_status", "generate_image", "list_images")
CHAT = ("server_status", "send_message")


def _add(gw, name, tools):
    gw.db.execute("INSERT INTO upstreams (name, url) VALUES (?, ?)", (name, f"http://127.0.0.1:1/{len(gw._links)}"))
    link = Link(gw.db.execute("SELECT * FROM upstreams WHERE name = ?", (name,)).fetchone())
    link.tools = [types.Tool(name=t, description=f"{t}.", inputSchema={"type": "object"}) for t in tools]
    gw._links[name] = link


def _gw(servers, defaults=()):
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('imagelibrary', 'hybrid', ?)",
                     (json.dumps(list(defaults)),))
    gw = Gateway({"default_group": "imagelibrary"}, database, None, "")
    for name, tools in servers.items():
        _add(gw, name, tools)
    gw.invalidate()
    return gw


def _defaults(gw):
    return json.loads(gw.db.execute("SELECT defaults FROM groups").fetchone()[0])


def _audit(gw):
    return [f"{r[0]}: {r[1]}" for r in gw.db.execute("SELECT kind, message FROM audit ORDER BY id")]


def test_a_shared_name_is_renamed_to_a_name_every_client_accepts():
    """"Image Generator__server_status" has a space: OpenAI-style clients refuse the whole list."""
    gw = _gw({"Image Generator": IMG, "Claude Chat": CHAT})
    assert {"Image_Generator__server_status", "Claude_Chat__server_status"} <= set(gw._exposed)
    assert "server_status" not in gw._exposed
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in gw._exposed), sorted(gw._exposed)
    assert gw.renamed() == [{"tool": "server_status", "servers": [
        {"upstream": "Claude Chat", "name": "Claude_Chat__server_status"},
        {"upstream": "Image Generator", "name": "Image_Generator__server_status"}]}]


def test_the_old_spellings_still_find_it():
    gw = _gw({"Image Generator": IMG, "Claude Chat": CHAT})
    for spelling in ("Image Generator__server_status", "image_generator/server_status",
                     "Image_Generator__server_status"):
        assert gw.resolve(spelling) == ("Image Generator", "server_status"), spelling


def test_a_new_server_does_not_break_a_groups_default():
    """The bug that started this: a second server with server_status turned a working default
    into "no tool by this name". Now the default follows the tool it named, both ways."""
    gw = _gw({"Image Generator": IMG}, ["server_status", "generate_image"])
    _add(gw, "Claude Chat", CHAT)
    gw.invalidate()
    assert _defaults(gw) == ["Image_Generator__server_status", "generate_image"]
    log = _audit(gw)
    assert any(e.startswith("rename: Claude Chat and Image Generator both have a tool named server_status") for e in log), log
    assert any('Group "imagelibrary": its default server_status is now called Image_Generator__server_status'
               in e for e in log), log

    del gw._links["Claude Chat"]           # the clash ends: the plain name comes back
    gw.invalidate()
    assert _defaults(gw) == ["server_status", "generate_image"]
    assert any("server_status is on one server again (Image Generator)" in e for e in _audit(gw))


def test_a_default_ambiguous_from_the_start_goes_where_the_group_lives():
    """A restart into an existing clash: nothing recorded which server's server_status was meant,
    so it goes to the server the group's other defaults come from -- and the audit says so."""
    gw = _gw({"Image Generator": IMG, "Claude Chat": CHAT}, ["server_status", "generate_image", "list_images"])
    assert _defaults(gw)[0] == "Image_Generator__server_status"
    assert any("picked Image Generator: the group's other defaults are there" in e for e in _audit(gw))


def test_with_nothing_to_go_on_it_stays_flagged_rather_than_guessed():
    gw = _gw({"Image Generator": IMG, "Claude Chat": CHAT}, ["server_status"])
    assert _defaults(gw) == ["server_status"] and gw.resolve("server_status") is None


def test_the_first_build_is_a_baseline_not_a_change():
    """A restart must not log every existing clash again as if it had just happened."""
    gw = _gw({"Image Generator": IMG, "Claude Chat": CHAT})
    assert _audit(gw) == [] and gw.renamed()


def test_renaming_a_server_carries_its_defaults_and_history_with_it():
    """A rename in the settings must show everywhere: a default spelled with the old server name
    follows (through the moment the old server is gone and the new one not yet loaded), the
    usage history moves so the stats and the adaptive ranking keep it, and the audit says so."""
    gw = _gw({"Old Name": IMG, "Claude Chat": CHAT}, ["Old_Name__server_status"])
    gw.db.execute("INSERT INTO events (ts, upstream, tool, status) VALUES ('t', 'Old Name', 'generate_image', 'ok')")
    gw.db.execute("UPDATE upstreams SET name = 'New Name' WHERE name = 'Old Name'")
    gw.rename_upstream("Old Name", "New Name")
    del gw._links["Old Name"]               # reconcile stops the old link first ...
    gw.invalidate()
    link = Link(gw.db.execute("SELECT * FROM upstreams WHERE name = 'New Name'").fetchone())
    link.tools = [types.Tool(name=t, description=f"{t}.", inputSchema={"type": "object"}) for t in IMG]
    gw._links["New Name"] = link            # ... then starts the new one
    gw.invalidate()
    assert _defaults(gw) == ["New_Name__server_status"]
    assert gw.db.execute("SELECT upstream FROM events").fetchone()[0] == "New Name"
    assert any(e.startswith('edit: Server "Old Name" was renamed to "New Name"') for e in _audit(gw)), _audit(gw)


def test_a_whole_mirrored_server_is_one_audit_line_not_sixty():
    """Registering a second copy of a server clashes every tool at once; the audit must stay
    readable -- one sentence saying what happened, with a few names and a count."""
    gw = _gw({"browser": [f"tool_{i}" for i in range(60)]})
    _add(gw, "mirror", [f"tool_{i}" for i in range(60)])
    gw.invalidate()
    log = [e for e in _audit(gw) if e.startswith("rename:")]
    assert len(log) == 1 and "60 tools with the same names" in log[0] and "and 57 more" in log[0], log
    del gw._links["mirror"]
    gw.invalidate()
    back = [e for e in _audit(gw) if e.startswith("rename:")][1:]
    assert len(back) == 1 and "60 tools are on one server again (browser" in back[0], back


def test_a_restart_does_not_report_standing_clashes_as_new():
    """At start the manager builds once with nothing loaded, then with every server's saved
    tools: that second build is the baseline, not a change."""
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('g', 'hybrid', '[]')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    gw.invalidate()                         # nothing loaded yet
    _add(gw, "Image Generator", IMG)
    _add(gw, "Claude Chat", CHAT)
    gw.invalidate()                         # the saved lists arrive together
    assert _audit(gw) == [] and gw.renamed()
