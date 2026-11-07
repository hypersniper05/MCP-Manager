"""SQLite is the source of truth for anything mutable.

Seeded from config on first run; after that the DB wins and edits to the config's
seed section are ignored — a UI needs somewhere to write, and generated secrets can
only ever be stored as hashes, so the file cannot be truth.

The model, in the words the settings page uses:

  group      what a client sees. Three modes:
               direct    every tool from every server is in the client's list
               hybrid    the group's defaults, plus search_tools and call_tool for the rest
               adaptive  hybrid, plus up to 10 tools learned per key from its calls (adaptive.py)
  key        membership in a group. The secret is stored hashed; shown once on create.
  upstream   a registered MCP server. Add, edit and remove here.
  tool_overrides  text we control for a tool, so a badly-described upstream tool can be
                  fixed from the manager. Feeds BOTH the embedding and what the client
                  sees — improving retrieval without improving the model's view of the
                  tool would be half the point.
  events     every call, so usage stats and the settings page have real data to show.
  audit      what the manager changed by itself: collision renames, defaults that followed them.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

MODES = ("direct", "hybrid", "adaptive")

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    mode     TEXT NOT NULL DEFAULT 'hybrid',     -- direct | hybrid | adaptive
    defaults TEXT NOT NULL DEFAULT '[]',         -- hybrid/adaptive: the tools listed up front
    adaptive_cap INTEGER,                        -- adaptive: learned tools listed (NULL = 10)
    servers  TEXT                                -- JSON list of the servers it reaches. NULL: all
                                                 -- of them, including any added later
);

CREATE TABLE IF NOT EXISTS upstreams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    timeout_ms  INTEGER,
    description TEXT,     -- admin override; else the server's own instructions go here
    headers     TEXT,     -- extra HTTP headers on every request to this server (JSON object),
                          -- for servers behind auth. Plain text in the DB: they must be sent
                          -- as-is, so they cannot be hashed like keys
    tools       TEXT,     -- the tool list it last answered with (JSON). Kept through outages and
                          -- restarts, so a down server's tools are still known by name
    shared      INTEGER NOT NULL DEFAULT 0   -- 1: one session per GROUP here, not per key
);

CREATE TABLE IF NOT EXISTS keys (
    id           INTEGER PRIMARY KEY,
    label        TEXT,
    group_id     INTEGER NOT NULL REFERENCES groups(id),
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_overrides (
    id          INTEGER PRIMARY KEY,
    upstream    TEXT NOT NULL,
    tool        TEXT NOT NULL,       -- the name the UPSTREAM gives it
    title       TEXT,
    description TEXT,               -- what gets embedded AND listed
    updated_at  TEXT NOT NULL,
    UNIQUE (upstream, tool)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    client_id  TEXT,          -- the key's label when it was called (what a person reads)
    key_id     INTEGER,       -- the key itself: a label can be renamed, and two can be alike
    group_id   INTEGER,
    upstream   TEXT,
    tool       TEXT,
    latency_ms REAL,
    status     TEXT
);
CREATE INDEX IF NOT EXISTS events_tool_idx  ON events (tool, ts);

-- What the manager changed on its own, for the settings page's audit: tool names renamed when two
-- servers share one, group defaults that followed a rename. Written rarely, never on a call.
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,      -- rename | default
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_group_idx ON events (group_id, ts);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def j(val, default="[]"):
    return json.dumps(val if val is not None else json.loads(default))


def default_label(plain: str) -> str:
    """What a key is called when nobody named it. Never the key itself: the label is listed
    back on every GET, and the secret must exist only in the response that created it."""
    return "key-" + hash_key(plain)[:8]


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    # Every tool call writes a usage row. In the default journal mode each of those commits is a
    # disk sync on the request path; WAL with NORMAL syncs at checkpoints instead, and a crash
    # can cost at most the last few usage rows -- never the config.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


def _migrate(db: sqlite3.Connection) -> None:
    cols = {r["name"] for r in db.execute("PRAGMA table_info(upstreams)")}
    if "tools" not in cols:
        db.execute("ALTER TABLE upstreams ADD COLUMN tools TEXT")
    if "headers" not in cols:
        db.execute("ALTER TABLE upstreams ADD COLUMN headers TEXT")
    if "shared" not in cols:
        db.execute("ALTER TABLE upstreams ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
    group_cols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "adaptive_cap" not in group_cols:
        db.execute("ALTER TABLE groups ADD COLUMN adaptive_cap INTEGER")
    if "servers" not in group_cols:
        db.execute("ALTER TABLE groups ADD COLUMN servers TEXT")
    _two_modes(db)
    _event_keys(db)
    # here, not in SCHEMA: on an old database the column only exists after _event_keys
    db.execute("CREATE INDEX IF NOT EXISTS events_key_idx ON events (key_id, id)")


def _event_keys(db: sqlite3.Connection) -> None:
    """Events used to name the key only by its label. The adaptive ranking learns per key, and
    a label can be renamed or shared, so events carry the key's id. Older rows are matched by
    label once: to the live key of that name if there is one, else the newest."""
    if "key_id" in {r["name"] for r in db.execute("PRAGMA table_info(events)")}:
        return
    db.execute("ALTER TABLE events ADD COLUMN key_id INTEGER")
    db.execute("UPDATE events SET key_id = (SELECT k.id FROM keys k WHERE k.label = events.client_id "
               "ORDER BY k.revoked, k.id DESC LIMIT 1)")


def _two_modes(db: sqlite3.Connection) -> None:
    """Bring a database from the four-mode era (manual/reveal/semantic/hybrid, with caps,
    exclusions and fixed lists) to the two modes. Runs once; a migrated DB has no `pinned`.

    The mapping keeps what each group actually showed as close as two modes allow:
    a manual group with no fixed list showed everything -> direct. Anything that narrowed
    the view -> hybrid, with its fixed list and its pins as the defaults, and search for
    the rest -- which is also what search already did for those groups.
    """
    cols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "pinned" not in cols:
        return
    rows = db.execute("SELECT * FROM groups").fetchall()
    db.execute("ALTER TABLE groups RENAME COLUMN pinned TO defaults")
    for r in rows:
        allowed = json.loads(r["allowed"]) if r["allowed"] else None
        if r["mode"] == "manual" and allowed is None:
            mode, defaults = "direct", []
        else:
            mode = "hybrid"
            defaults = list(dict.fromkeys(json.loads(r["pinned"] or "[]") + (allowed or [])))
        db.execute("UPDATE groups SET mode = ?, defaults = ? WHERE id = ?",
                   (mode, j(defaults), r["id"]))
    for col in ("allowed", "excluded", "max_tools", "idle_minutes"):
        if col in cols:
            db.execute(f"ALTER TABLE groups DROP COLUMN {col}")
    # the only rows it ever held were reveal's idle window and a copy of boot config
    db.execute("DROP TABLE IF EXISTS settings")
    db.execute("DROP INDEX IF EXISTS events_activity_idx")


def seed(db: sqlite3.Connection, cfg: dict) -> bool:
    """Load the config's seed section on first run only. Returns True if it seeded."""
    if db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]:
        return False

    name_to_id = {}
    for g in cfg.get("groups") or []:
        mode = g.get("mode", "hybrid")
        if mode not in MODES:
            raise ValueError(f"group {g['name']}: mode must be one of {MODES}, not {mode!r}")
        cur = db.execute("INSERT INTO groups (name, mode, defaults, adaptive_cap, servers) VALUES (?,?,?,?,?)",
                         (g["name"], mode, j(g.get("defaults")), g.get("adaptive_cap"),
                          None if g.get("servers") is None else j(g["servers"])))
        name_to_id[g["name"]] = cur.lastrowid

    for k in cfg.get("keys") or []:
        db.execute(
            "INSERT INTO keys (label, group_id, key_hash, created_at) VALUES (?,?,?,?)",
            (k.get("label") or default_label(k["key"]), name_to_id[k["group"]],
             hash_key(k["key"]), now()),
        )

    for u in cfg.get("upstreams") or []:
        db.execute("INSERT INTO upstreams (name, url, enabled, description, headers, shared) VALUES (?,?,?,?,?,?)",
                   (u["name"], u["url"], 1 if u.get("enabled", True) else 0, u.get("description"),
                    j(u["headers"]) if u.get("headers") else None, 1 if u.get("shared") else 0))

    db.commit()
    return True


# ---- read paths -------------------------------------------------------------

def group_for_key(db: sqlite3.Connection, plain: str):
    return db.execute(
        "SELECT k.id AS key_id, k.label, g.* FROM keys k JOIN groups g ON g.id = k.group_id "
        "WHERE k.key_hash = ? AND k.revoked = 0",
        (hash_key(plain),),
    ).fetchone()


def group_desc(db, upstream: str, tool: str) -> str | None:
    row = db.execute(
        "SELECT description FROM tool_overrides WHERE upstream = ? AND tool = ?",
        (upstream, tool),
    ).fetchone()
    return row["description"] if row else None


# ---- write paths -------------------------------------------------------------

def touch_key(db, key_id: int) -> None:
    """Stamp last_used_at, at most once a minute. One MCP session is many HTTP requests,
    and the settings page only needs to know whether an app is still connecting."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    cur = db.execute("UPDATE keys SET last_used_at = ? WHERE id = ? "
                     "AND (last_used_at IS NULL OR last_used_at < ?)", (now(), key_id, stale))
    if cur.rowcount:
        db.commit()


def log_event(db, client_id, group_id, upstream, tool, latency_ms, status, key_id=None) -> None:
    db.execute(
        "INSERT INTO events (ts, client_id, key_id, group_id, upstream, tool, latency_ms, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now(), client_id, key_id, group_id, upstream, tool, latency_ms, status),
    )
    db.commit()  # buffered writes would need batching; at this scale this is fine and honest


def audit(db, kind: str, message: str) -> None:
    db.execute("INSERT INTO audit (ts, kind, message) VALUES (?,?,?)", (now(), kind, message))
    db.commit()


def server_moved(db, old: str, new: str | None) -> None:
    """Groups that chose their servers name them: follow a rename, or let a removed server go."""
    for g in db.execute("SELECT id, servers FROM groups WHERE servers IS NOT NULL").fetchall():
        names = json.loads(g["servers"])
        if old in names:
            names = sorted({new if n == old else n for n in names} - {None})
            db.execute("UPDATE groups SET servers = ? WHERE id = ?", (json.dumps(names), g["id"]))
    db.commit()


def set_override(db, upstream: str, tool: str, description: str | None, title: str | None = None) -> None:
    if description is None and title is None:
        # clearing it is removing it: an empty row would still list as an override
        db.execute("DELETE FROM tool_overrides WHERE upstream = ? AND tool = ?", (upstream, tool))
        db.commit()
        return
    db.execute(
        "INSERT INTO tool_overrides (upstream, tool, title, description, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT (upstream, tool) DO UPDATE SET title = excluded.title, "
        "description = excluded.description, updated_at = excluded.updated_at",
        (upstream, tool, title, description, now()),
    )
    db.commit()  # -> caller decides whether this triggers a re-embed
