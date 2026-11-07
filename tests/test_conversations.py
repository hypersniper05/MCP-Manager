"""What the gateway believes a client has in its own tool list -- deterministic, no network.

A server opened by use joins a hybrid session's list, but only for a client that re-lists. Until
it does, the tool is callable only through call_tool, and every piece of advice has to name the
call that client can actually make.
"""

import time

import mcp.types as types

from mcp_manager import db
from mcp_manager.gateway import Gateway, Link


def _gateway():
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('g', 'hybrid', ?)", ('["screenshot"]',))
    database.execute("INSERT INTO upstreams (name, url) VALUES ('browser', 'http://127.0.0.1:1/mcp')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    link = Link(database.execute("SELECT * FROM upstreams").fetchone())
    link.tools = [types.Tool(name=n, inputSchema={"type": "object"}) for n in ("screenshot", "navigate", "list_tabs")]
    gw._links["browser"] = link
    gw.invalidate()
    return gw, gw.group("g")


def test_a_server_opened_since_the_last_list_is_not_in_the_clients_list():
    gw, group = _gateway()
    gw._listed["c"] = time.monotonic()           # the client listed at init
    gw._use("c", "browser")                      # then used browser, which opened it
    assert gw._in_client_list(group, "c") == {"screenshot"}, "only the defaults it was handed"
    assert "navigate" in gw.members(group, "c"), "a re-list would hand it the whole server"


def test_after_a_relist_the_opened_server_is_in_the_clients_list():
    gw, group = _gateway()
    gw._listed["c"] = time.monotonic()
    gw._use("c", "browser")
    gw._listed["c"] = time.monotonic() + 1       # the client re-listed after the open
    assert {"navigate", "list_tabs", "screenshot"} <= gw._in_client_list(group, "c")


def test_conversations_do_not_share_what_they_opened():
    gw, group = _gateway()
    gw._use("a", "browser")
    assert "navigate" in gw.members(group, "a") and "navigate" not in gw.members(group, "b")
