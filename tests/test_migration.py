"""An existing database from the four-mode era must come up in the two-mode world with its
keys intact -- deleting the DB to upgrade would mean re-issuing every app's key."""

import json
import sqlite3

from mcp_manager import db

OLD_GROUPS = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, mode TEXT NOT NULL DEFAULT 'manual',
    allowed TEXT, pinned TEXT NOT NULL DEFAULT '[]', excluded TEXT NOT NULL DEFAULT '[]',
    max_tools INTEGER, idle_minutes INTEGER);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_old_groups_map_onto_direct_and_hybrid(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript(OLD_GROUPS)
    rows = [  # name, mode, allowed, pinned, max_tools
        ("everything", "manual", None, "[]", None),
        ("capped", "manual", None, "[]", 30),
        ("narrow", "manual", '["a", "b"]', '["c"]', None),
        ("searchy", "semantic", None, '["x"]', None),
        ("revealing", "reveal", None, '["y"]', None),
    ]
    con.executemany("INSERT INTO groups (name, mode, allowed, pinned, max_tools) VALUES (?,?,?,?,?)", rows)
    con.execute("INSERT INTO settings VALUES ('reveal_idle_minutes', '120')")
    con.commit()
    con.close()

    database = db.connect(path)
    got = {r["name"]: (r["mode"], json.loads(r["defaults"])) for r in database.execute("SELECT * FROM groups")}
    assert got == {
        "everything": ("direct", []),
        "capped": ("direct", []),          # the cap was the bug, not the intent
        "narrow": ("hybrid", ["c", "a", "b"]),
        "searchy": ("hybrid", ["x"]),
        "revealing": ("hybrid", ["y"]),
    }
    cols = {r["name"] for r in database.execute("PRAGMA table_info(groups)")}
    # the old four-mode columns are gone; adaptive_cap is the one later migrations add
    assert cols == {"id", "name", "mode", "defaults", "adaptive_cap", "servers"}
    assert not database.execute("SELECT name FROM sqlite_master WHERE name = 'settings'").fetchone()

    database.close()
    db.connect(path).close()               # and a second start is a no-op, not an error


def test_an_old_upstreams_table_gains_the_new_columns(tmp_path):
    """The config grows by adding columns (tools, then headers); an existing database must
    come up with them empty, not with its registered servers lost."""
    path = str(tmp_path / "old-ups.db")
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE upstreams (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, timeout_ms INTEGER, description TEXT);
""")
    con.execute("INSERT INTO upstreams (name, url) VALUES ('x', 'http://127.0.0.1:1/mcp')")
    con.commit()
    con.close()

    database = db.connect(path)
    row = database.execute("SELECT * FROM upstreams").fetchone()
    assert row["name"] == "x" and row["tools"] is None and row["headers"] is None
    database.close()
