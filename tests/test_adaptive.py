"""Adaptive mode -- deterministic, no network.

The ranking is held to what was promised: it starts empty, a tool earns its place with use,
the list never passes ten, an unused tool leaves, a likelier one takes the weakest slot (and a
near-tie does not churn the list), a chain pulls in the tool that usually comes next, and the
events log alone is enough to relearn all of it after a restart.
"""

import asyncio
import time

import mcp.types as types

from mcp_manager import db, gate
from mcp_manager.adaptive import CAP, Adaptive, Model
from mcp_manager.gateway import Gateway, Link


def T(name):
    return ("srv", name)


# ---- the ranking ---------------------------------------------------------------------------

def test_a_new_key_starts_empty_and_one_call_is_not_a_habit():
    m = Model()
    assert m.picks == []
    m.observe(T("a"))
    assert m.picks == [], "one use scores 2.0, under ENTER: a tool called once is not listed yet"
    m.observe(T("a"))
    assert m.picks == [T("a")]


def test_the_list_never_holds_more_than_ten():
    m = Model()
    for i in range(30):
        for _ in range(3):
            m.observe(T(f"t{i}"))
    assert len(m.picks) == CAP == 10


def test_a_tool_that_falls_out_of_use_leaves():
    m = Model()
    for _ in range(3):
        m.observe(T("old"))
    assert T("old") in m.picks
    for i in range(400):                 # the app moves on to other work
        m.observe(T(f"new{i % 5}"))
    assert T("old") not in m.picks
    assert set(m.picks) == {T(f"new{i}") for i in range(5)}


def test_a_likelier_tool_takes_the_weakest_slot_and_a_near_tie_does_not():
    m = Model()
    m.picks = [T(f"p{i}") for i in range(10)]
    m.slow = {t: 2.0 for t in m.picks}
    m.slow[T("p9")] = 1.9                # the weakest member
    m.slow[T("c")] = 2.2                 # better, but within MARGIN: no churn for a near-tie
    assert not m.reselect() and T("c") not in m.picks
    m.slow[T("c")] = 2.5                 # more than 20% ahead of the weakest
    assert m.reselect()
    assert m.picks[9] == T("c") and m.picks[:9] == [T(f"p{i}") for i in range(9)], \
        "it takes the weakest's slot; everything else keeps its place in the client's list"


def test_a_chain_pulls_in_the_tool_that_usually_comes_next():
    chained = Model()
    for _ in range(10):
        chained.observe(T("generate"))
    chained.observe(T("get_job"))        # once, but right after a tool the app lives on
    assert T("get_job") in chained.picks

    alone = Model()
    for _ in range(10):
        alone.observe(T("generate"))
    alone.observe(T("other"))
    alone.observe(T("get_job"))          # once, after a tool used once
    assert T("get_job") not in alone.picks


def test_the_groups_own_defaults_never_take_a_slot():
    m = Model()
    for _ in range(5):
        m.observe(T("pinned"), exclude={T("pinned")})
        m.observe(T("b"), exclude={T("pinned")})
    assert m.picks == [T("b")]


def test_the_log_alone_relearns_everything():
    seq = [(1, "srv", f"t{(i * 7) % 13}") for i in range(300)] + [(2, "srv", "x"), (2, "srv", "x")]
    live = Adaptive()
    for key_id, up, tool in seq:
        live.observe(key_id, (up, tool))
    again = Adaptive()
    again.rebuild(seq)
    assert again.picks(1) == live.picks(1) and again.picks(2) == [("srv", "x")]
    assert again.ranked(1) == live.ranked(1)


# ---- in the gateway ------------------------------------------------------------------------

def _gateway(mode="adaptive", path=":memory:", defaults='["screenshot"]'):
    database = db.connect(path)
    if not database.execute("SELECT 1 FROM groups").fetchone():
        database.execute("INSERT INTO groups (name, mode, defaults) VALUES ('g', ?, ?)", (mode, defaults))
        database.execute("INSERT INTO keys (label, group_id, key_hash, created_at) VALUES ('app', 1, 'h', 'now')")
        database.execute("INSERT INTO upstreams (name, url) VALUES ('browser', 'http://127.0.0.1:1/mcp')")
    gw = Gateway({"default_group": "g"}, database, None, "")
    link = Link(database.execute("SELECT * FROM upstreams WHERE name = 'browser'").fetchone())
    link.tools = [types.Tool(name=n, description=f"{n} the page", inputSchema={"type": "object"})
                  for n in ("screenshot", "navigate", "click", "read_text")]
    gw._links["browser"] = link
    gw.invalidate()
    return gw


def _as_key(fn):
    k, g = gate.current_key.set(1), gate.current_group.set("g")
    try:
        return fn()
    finally:
        gate.current_key.reset(k)
        gate.current_group.reset(g)


def _listed(gw):
    return _as_key(lambda: [t.name for t in asyncio.run(gw._list_tools(None, None)).tools])


def _call(gw, tool, times=1):
    for _ in range(times):
        _as_key(lambda: gw._log(gw.group("g"), ("browser", tool), time.monotonic(), "ok"))


def test_an_adaptive_group_lists_what_its_app_uses_after_the_defaults():
    gw = _gateway()
    assert _listed(gw)[:1] == ["screenshot"] and "navigate" not in _listed(gw)
    _call(gw, "navigate", 2)
    _call(gw, "screenshot", 3)            # a default: it is listed already, never twice
    names = _listed(gw)
    assert names[:2] == ["screenshot", "navigate"], names
    assert names.count("screenshot") == 1 and "search_tools" in names and "call_tool" in names


def test_a_hybrid_group_learns_too_but_lists_only_its_defaults():
    gw = _gateway(mode="hybrid")
    _call(gw, "navigate", 3)
    assert "navigate" not in _listed(gw)
    assert gw.adaptive.picks(1) == [("browser", "navigate")], "switching to adaptive starts warm"


def test_what_the_client_has_is_what_it_was_given_not_what_it_would_get_now():
    gw = _gateway()
    _listed(gw)                           # the client lists before it has learned anything
    _call(gw, "navigate", 2)
    have = _as_key(lambda: gw._in_client_list(gw.group("g"), "key:1"))
    assert "navigate" not in have, "advice must say call_tool until the client lists again"
    _listed(gw)
    assert "navigate" in _as_key(lambda: gw._in_client_list(gw.group("g"), "key:1"))


def test_a_restart_relearns_from_the_events_log(tmp_path):
    path = str(tmp_path / "m.db")
    gw = _gateway(path=path)
    _call(gw, "click", 2)
    _call(gw, "read_text", 2)
    before = gw.adaptive.picks(1)
    again = _gateway(path=path)
    again.learn()
    assert again.adaptive.picks(1) == before == [("browser", "click"), ("browser", "read_text")]


def test_the_statistics_page_gets_chains_hours_apps_and_rankings():
    gw = _gateway()
    for tool in ("navigate", "screenshot", "navigate", "screenshot", "click"):
        _call(gw, tool)
    out = gw.stats_detail(30)
    assert out["calls"] == 5 and out["status"] == {"ok": 5}
    assert sum(map(sum, out["heat"])) == 5 and len(out["heat"]) == 7 and len(out["heat"][0]) == 24
    top = out["chains"][0]
    assert (top["from"]["tool"], top["to"]["tool"], top["count"]) == ("navigate", "screenshot", 2)
    app = out["apps"][0]
    assert app["label"] == "app" and app["mode"] == "adaptive" and app["tools_used"] == 3
    assert {t["tool"] for t in app["adaptive"] if t["picked"]} == {"navigate"}, \
        "screenshot is a default; click was used once"
    assert out["tools"][0]["calls"] == 2 and out["servers"][0]["upstream"] == "browser"


def test_old_events_are_matched_to_their_key_by_label(tmp_path):
    """Before key_id, events named the key by label only. A label two keys share goes to the
    live one, not the revoked one it replaced."""
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, mode TEXT, defaults TEXT);
        CREATE TABLE keys (id INTEGER PRIMARY KEY, label TEXT, group_id INTEGER, key_hash TEXT UNIQUE,
                           created_at TEXT, last_used_at TEXT, revoked INTEGER DEFAULT 0);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, client_id TEXT, group_id INTEGER,
                             upstream TEXT, tool TEXT, latency_ms REAL, status TEXT);
        INSERT INTO groups VALUES (1, 'g', 'hybrid', '[]');
        INSERT INTO keys VALUES (1, 'llama', 1, 'a', 'x', NULL, 1), (2, 'llama', 1, 'b', 'x', NULL, 0),
                                (3, 'pi', 1, 'c', 'x', NULL, 0);
        INSERT INTO events (ts, client_id, upstream, tool) VALUES ('t', 'llama', 's', 'a'), ('t', 'pi', 's', 'b'),
                                                                   ('t', 'gone', 's', 'c');
    """)
    old.commit()
    old.close()
    d = db.connect(path)
    assert [tuple(r) for r in d.execute("SELECT client_id, key_id FROM events ORDER BY id")] == \
        [("llama", 2), ("pi", 3), ("gone", None)]


# ---- the cap, per group ------------------------------------------------------------------

def test_a_group_can_list_fewer_or_more_learned_tools():
    m = Model()
    for i in range(30):
        for _ in range(3):
            m.observe(T(f"t{i}"), cap=3)
    assert len(m.picks) == 3
    weakest = min(m.picks, key=lambda t: m.scores()[t])
    m.reselect(cap=2)                       # lowered: the weakest goes first
    assert len(m.picks) == 2 and weakest not in m.picks
    m.reselect(cap=25)                      # raised: the next in line come in, no new call needed
    assert len(m.picks) == 25


def test_the_gateway_lists_as_many_as_the_group_says_and_follows_a_change_at_once():
    gw = _gateway(defaults="[]")
    for tool in ("screenshot", "navigate", "click", "read_text"):
        _call(gw, tool, 2)
    assert len([n for n in _listed(gw) if n not in NATIVE]) == 4          # the default, 10, holds all four
    gw.db.execute("UPDATE groups SET adaptive_cap = 2")
    gw.recap()
    assert len([n for n in _listed(gw) if n not in NATIVE]) == 2
    assert gw.adaptive_view(1, gw.group("g"))["cap"] == 2


NATIVE = {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}
