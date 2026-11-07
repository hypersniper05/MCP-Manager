"""Management API — the surface a settings page is built on. The portal is just one client
of it; the README's "Management API" section is the contract for wiring up your own.

Starlette routes on the same process and port as MCP (via custom_starlette_routes),
not FastAPI: FastAPI would be a dependency doing what thirty lines here already do.

Auth is its own: the admin token, never an MCP client key, and it is checked in exactly one
place -- the gate, for every path under /api/. Keys live in prompts and config files and can
be replayed by anything holding them; the boundary is that a key buys a tool view, and only
the admin token buys the right to change the rules. Nothing here is reachable from MCP.

Secrets are write-only in practice: a generated key is returned once on create and is
never recoverable afterwards, only revoked or deleted.
"""

import json
import re
import secrets
import sqlite3

import anyio

from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import adaptive, db


class BadRequest(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _handler(fn):
    """Every route answers {"error": ...} with a real status, never a stack trace."""
    async def wrapped(request):
        try:
            return await fn(request)
        except BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        except sqlite3.IntegrityError as e:
            return JSONResponse({"error": _conflict(e)}, status_code=409)
    return wrapped


def _conflict(e) -> str:
    """'UNIQUE constraint failed: groups.name' is SQLite talking; a person adding a group
    needs to hear that the name is taken. Anything else keeps SQLite's words."""
    m = re.search(r"UNIQUE constraint failed: (\w+)\.(\w+)", str(e))
    noun = {"groups": "group", "upstreams": "server", "keys": "key"}.get(m.group(1)) if m else None
    return f"name taken: there is already a {noun} with that {m.group(2)}" if noun else f"conflict: {e}"


async def _body(request) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        raise BadRequest("body is not valid JSON")
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


def _name(value, what="name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"{what} required")
    return value.strip()


def _mode(value) -> str:
    if value not in db.MODES:
        raise BadRequest(f"mode must be one of {list(db.MODES)}")
    return value


def _cap(value):
    """How many learned tools an adaptive group lists: a whole number 1-50, or null for the
    default (10, the measured one)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= adaptive.MAX_CAP:
        raise BadRequest(f"adaptive_cap must be a whole number from 1 to {adaptive.MAX_CAP}, "
                         f"or null for the default ({adaptive.CAP})")
    return value


def _group_servers(request, value):
    """The servers a group reaches, as stored: null for all of them (new ones too), else a list
    of registered names. An empty list is allowed: a group that reaches nothing yet."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest("servers must be a list of server names, or null for all of them")
    known = {r["name"] for r in request.app.state.db.execute("SELECT name FROM upstreams")}
    unknown = sorted(set(value) - known)
    if unknown:
        raise BadRequest(f"no server called {unknown[0]!r}")
    return json.dumps(sorted(set(value)))


def _tool_list(value) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest("defaults must be a list of tool names")
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def _url(value) -> str:
    value = _name(value, "url")
    if not value.startswith(("http://", "https://")):
        raise BadRequest("url must be an http:// or https:// MCP endpoint")
    return value


def _headers(value):
    """Extra headers for a server, as stored: a JSON object of strings, or None for none.
    Plain text in the DB on purpose — they must be sent to the upstream as-is, so unlike
    keys they cannot be hashed. The admin token is the only thing that reads them back."""
    if value in (None, {}, ""):
        return None
    if not isinstance(value, dict):
        raise BadRequest("headers must be an object of name -> value, "
                         'e.g. {"Authorization": "Bearer <token>"}')
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            raise BadRequest("header names must be non-empty strings")
        if not isinstance(v, str):
            raise BadRequest(f"header {k.strip()!r}: the value must be a string")
        out[k.strip()] = v
    return json.dumps(out)


def _group_row(request, name):
    g = request.app.state.db.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()
    if not g:
        raise BadRequest("no such group", 404)
    return g


# ---- groups ------------------------------------------------------------------

def _group_out(request, row):
    gw, database = request.app.state.gateway, request.app.state.db
    defaults = json.loads(row["defaults"] or "[]")
    return {
        "id": row["id"], "name": row["name"], "mode": row["mode"], "defaults": defaults,
        # learned tools an adaptive group lists (the setting in effect: 10 unless changed)
        "adaptive_cap": row["adaptive_cap"] or adaptive.CAP,
        # the servers it reaches: null is all of them, including any added later
        "servers": None if row["servers"] is None else json.loads(row["servers"]),
        # defaults that lead nowhere right now: a server down or not in the group, a tool renamed
        "unresolved": gw.unresolved(row),
        "sees": gw.live_view(row),
        "keys": [k["label"] for k in database.execute(
            "SELECT label FROM keys WHERE group_id = ? AND revoked = 0", (row["id"],))],
    }


@_handler
async def list_groups(request):
    rows = request.app.state.db.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return JSONResponse([_group_out(request, r) for r in rows])


@_handler
async def create_group(request):
    body = await _body(request)
    database = request.app.state.db
    cur = database.execute("INSERT INTO groups (name, mode, defaults, adaptive_cap, servers) VALUES (?,?,?,?,?)",
                           (_name(body.get("name")), _mode(body.get("mode", "hybrid")),
                            db.j(_tool_list(body.get("defaults", []))), _cap(body.get("adaptive_cap")),
                            _group_servers(request, body.get("servers"))))
    database.commit()
    return JSONResponse(_group_out(request, database.execute(
        "SELECT * FROM groups WHERE id = ?", (cur.lastrowid,)).fetchone()), status_code=201)


@_handler
async def update_group(request):
    database = request.app.state.db
    g = _group_row(request, request.path_params["name"])
    if request.method == "DELETE":
        # The keys go with the group. A key pointing at a group that no longer exists cannot
        # resolve to a view, so leaving it would only hand someone a connection that silently
        # sees nothing -- better to say plainly that deleting the group deletes its keys.
        ids = [r["id"] for r in database.execute("SELECT id FROM keys WHERE group_id = ?", (g["id"],))]
        gone = database.execute("DELETE FROM keys WHERE group_id = ?", (g["id"],)).rowcount
        database.execute("DELETE FROM groups WHERE id = ?", (g["id"],))
        database.commit()
        for key_id in ids:
            request.app.state.gateway.close_clients(key_id=key_id)
        request.app.state.gateway.close_clients(group_id=g["id"])   # and its shared sessions
        return JSONResponse({"ok": True, "deleted_keys": gone})
    body = await _body(request)
    fields = {}
    if "name" in body:
        fields["name"] = _name(body["name"])
    if "mode" in body:
        fields["mode"] = _mode(body["mode"])
    if "defaults" in body:
        fields["defaults"] = db.j(_tool_list(body["defaults"]))
    if "adaptive_cap" in body:
        fields["adaptive_cap"] = _cap(body["adaptive_cap"])
    if "servers" in body:
        fields["servers"] = _group_servers(request, body["servers"])
    if fields:
        database.execute(f"UPDATE groups SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                         (*fields.values(), g["id"]))
        database.commit()
    if {"adaptive_cap", "defaults", "servers"} & set(fields):
        request.app.state.gateway.recap()      # its apps' learned lists follow at once
    if "servers" in fields:
        request.app.state.gateway.close_unreachable()
    if fields.get("name", g["name"]) != g["name"]:
        # Keys, events and rankings point at the group's id, so they follow by themselves. The one
        # thing that names it is the boot config's default group: follow it for this run.
        gw, note = request.app.state.gateway, ""
        if gw.cfg.get("default_group") == g["name"]:
            gw.cfg["default_group"] = fields["name"]
            note = (" It is the default group: set default_group in config.yaml to the new name too, "
                    "or a key created without a group has nowhere to go after a restart.")
        db.audit(database, "edit", f'Group "{g["name"]}" was renamed to "{fields["name"]}". '
                 f"Its keys, defaults and usage stay with it.{note}")
    return JSONResponse(_group_out(request, database.execute(
        "SELECT * FROM groups WHERE id = ?", (g["id"],)).fetchone()))


# ---- upstreams ---------------------------------------------------------------

def _upstream_out(request, name):
    return next((u for u in request.app.state.gateway.upstreams_state() if u["name"] == name), None)


@_handler
async def list_upstreams(request):
    return JSONResponse(request.app.state.gateway.upstreams_state())


@_handler
async def add_upstream(request):
    body = await _body(request)
    database = request.app.state.db
    name = _name(body.get("name"))
    database.execute(
        "INSERT INTO upstreams (name, url, enabled, timeout_ms, description, headers, shared) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, _url(body.get("url")), 1 if body.get("enabled", True) else 0,
         body.get("timeout_ms"), (body.get("description") or "").strip() or None,
         _headers(body.get("headers")), 1 if body.get("shared") else 0))
    database.commit()
    # waits (bounded) for the first connection attempt, so the answer says what happened
    await request.app.state.gateway.reconcile()
    return JSONResponse(_upstream_out(request, name), status_code=201)


@_handler
async def update_upstream(request):
    database, old = request.app.state.db, request.path_params["name"]
    row = database.execute("SELECT * FROM upstreams WHERE name = ?", (old,)).fetchone()
    if not row:
        raise BadRequest("no such server", 404)
    if request.method == "DELETE":
        database.execute("DELETE FROM upstreams WHERE id = ?", (row["id"],))
        database.commit()
        db.server_moved(database, old, None)
        await request.app.state.gateway.reconcile()
        return JSONResponse({"ok": True})
    body = await _body(request)
    fields = {}
    if "name" in body:
        fields["name"] = _name(body["name"])
    if "url" in body:
        fields["url"] = _url(body["url"])
        if fields["url"] != row["url"]:
            fields["tools"] = None      # another address may be another server: forget its tools
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    if "timeout_ms" in body:
        fields["timeout_ms"] = body["timeout_ms"]
    if "description" in body:
        # blank means "use the server's own words again", not "say nothing"
        fields["description"] = (body["description"] or "").strip() or None
    if "headers" in body:
        fields["headers"] = _headers(body["headers"])   # {} or null clears them
    if "shared" in body:
        # one session per group instead of per key: keys in a group share this server's state
        fields["shared"] = 1 if body["shared"] else 0
    if fields:
        database.execute(f"UPDATE upstreams SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                         (*fields.values(), row["id"]))
        if fields.get("name", old) != old:
            # overrides are keyed by server name; a rename must not orphan them
            database.execute("UPDATE tool_overrides SET upstream = ? WHERE upstream = ?",
                             (fields["name"], old))
        database.commit()
        if fields.get("name", old) != old:
            # and neither may its usage history, its learned rankings or the defaults naming it
            request.app.state.gateway.rename_upstream(old, fields["name"])
        await request.app.state.gateway.reconcile()
        # a server's name and description are part of every one of its tools' documents
        await request.app.state.gateway.reembed()
    return JSONResponse(_upstream_out(request, fields.get("name", old)))


@_handler
async def reconnect_upstream(request):
    """Reconnect one server now: a new session with its current headers, its tools listed
    again. The answer is the server as it stands after the first attempt."""
    name = request.path_params["name"]
    row = request.app.state.db.execute("SELECT enabled FROM upstreams WHERE name = ?", (name,)).fetchone()
    if not row:
        raise BadRequest("no such server", 404)
    if not row["enabled"]:
        raise BadRequest(f"{name} is turned off: turn it on and it connects by itself")
    await request.app.state.gateway.reconnect(name)
    return JSONResponse(_upstream_out(request, name))


# A test click should answer before the person forgets why they clicked. A server that
# legitimately needs longer gets its full timeout once it is actually added.
TEST_CONNECT_S = 15


@_handler
async def test_connection(request):
    """Try an endpoint before saving it -- what the portal's Test connection button needs
    while adding a server. One session: connect, initialize, list tools, close. Nothing is
    stored and no link is started, so a wrong URL costs one refused connection rather than
    a server that retries forever in the background."""
    body = await _body(request)
    url = _url(body.get("url"))
    headers = json.loads(_headers(body.get("headers")) or "{}")
    from mcp import ClientSession                          # here, not at the top: only this
    from mcp.client.streamable_http import streamable_http_client   # route speaks to upstreams
    from .gateway import (_auth_client, _close_auth_client, _reason,  # same words as the health panel
                          SSE_EVENT_MAX)
    http = None
    try:
        http = _auth_client(headers)
        with anyio.move_on_after(TEST_CONNECT_S) as scope:
            async with streamable_http_client(url, http_client=http,
                                              max_sse_event_size=SSE_EVENT_MAX) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = (await session.list_tools()).tools
                    name = getattr(getattr(init, "server_info", None), "title", None)
                    return JSONResponse({"ok": True, "tools": len(tools), "server": name})
        if scope.cancelled_caught:
            return JSONResponse({"ok": False,
                                 "error": f"no answer within {TEST_CONNECT_S} s — wrong address, "
                                          "or a server that is not running"})
    except Exception as e:   # noqa: BLE001 -- any answer beats a stack trace here
        return JSONResponse({"ok": False, "error": _reason(e)})
    finally:
        await _close_auth_client(http)


# ---- tool overrides (what gets embedded AND what the client reads) ----------

@_handler
async def list_tools(request):
    return JSONResponse(request.app.state.gateway.all_tools())


@_handler
async def list_overrides(request):
    return JSONResponse(request.app.state.gateway.overrides_state())


@_handler
async def set_override(request):
    body = await _body(request)
    description = (body.get("description") or "").strip() or None
    title = (body.get("title") or "").strip() or None
    db.set_override(request.app.state.db, _name(body.get("upstream"), "upstream"),
                    _name(body.get("tool"), "tool"), description, title)
    # a description or title edit changes the embedded document: re-embed it, and answer only
    # once that is done, so the very next search already reflects the edit
    await request.app.state.gateway.reembed()
    return JSONResponse({"ok": True, "reindex": True})


# ---- keys (membership) ------------------------------------------------------

def _key_row(request):
    try:
        key_id = int(request.path_params["id"])
    except ValueError:
        raise BadRequest("no such key", 404)
    row = request.app.state.db.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
    if not row:
        raise BadRequest("no such key", 404)
    return row


@_handler
async def list_keys(request):
    rows = request.app.state.db.execute(
        # group_name, not AS group — GROUP is reserved in SQLite
        "SELECT k.id, k.label, g.name AS group_name, k.created_at, k.last_used_at, k.revoked, "
        "substr(k.key_hash,1,8) AS hint FROM keys k JOIN groups g ON g.id = k.group_id ORDER BY k.label"
    ).fetchall()
    return JSONResponse([{**dict(r), "revoked": bool(r["revoked"])} for r in rows])


@_handler
async def create_key(request):
    body = await _body(request)
    g = _group_row(request, body.get("group", request.app.state.gateway.cfg["default_group"]))
    plain = "mcpm_" + secrets.token_urlsafe(24)
    label = (body.get("label") or "").strip() or db.default_label(plain)
    cur = request.app.state.db.execute(
        "INSERT INTO keys (label, group_id, key_hash, created_at) VALUES (?,?,?,?)",
        (label, g["id"], db.hash_key(plain), db.now()))
    request.app.state.db.commit()
    # The only time the secret exists anywhere it can be read back.
    return JSONResponse({"id": cur.lastrowid, "key": plain, "label": label, "group": g["name"]},
                        status_code=201)


@_handler
async def update_key(request):
    """Move a key to another group, or rename it -- without re-issuing it, which would mean
    reconfiguring the app that holds it."""
    k, database = _key_row(request), request.app.state.db
    if request.method == "DELETE":
        database.execute("DELETE FROM keys WHERE id = ?", (k["id"],))
        database.commit()
        request.app.state.gateway.close_clients(key_id=k["id"])   # its sessions upstream go too
        request.app.state.gateway.adaptive.forget(k["id"])        # and what it had learned
        return JSONResponse({"ok": True})
    body = await _body(request)
    if "group" in body:
        database.execute("UPDATE keys SET group_id = ? WHERE id = ?",
                         (_group_row(request, body["group"])["id"], k["id"]))
        database.commit()
        request.app.state.gateway.recap()      # the new group's cap and defaults apply now
        request.app.state.gateway.close_unreachable()
    if "label" in body:
        database.execute("UPDATE keys SET label = ? WHERE id = ?", (_name(body["label"], "label"), k["id"]))
    database.commit()
    return JSONResponse({"ok": True})


@_handler
async def revoke_key(request):
    k = _key_row(request)
    request.app.state.db.execute("UPDATE keys SET revoked = 1 WHERE id = ?", (k["id"],))
    request.app.state.db.commit()
    request.app.state.gateway.close_clients(key_id=k["id"])       # its sessions upstream go too
    return JSONResponse({"ok": True})


@_handler
async def reissue_key(request):
    """A new secret for the same row: the label and the group stay, so the app is reconfigured
    with one new key rather than a deleted-and-recreated one. A lost secret (shown once) is
    recovered this way. A revoked row comes back — a re-issued key is by definition a live one."""
    k, database = _key_row(request), request.app.state.db
    plain = "mcpm_" + secrets.token_urlsafe(24)
    database.execute("UPDATE keys SET key_hash = ?, revoked = 0 WHERE id = ?",
                     (db.hash_key(plain), k["id"]))
    database.commit()
    request.app.state.gateway.close_clients(key_id=k["id"])       # the old secret is dead now
    g = database.execute("SELECT name FROM groups WHERE id = ?", (k["group_id"],)).fetchone()
    return JSONResponse({"id": k["id"], "key": plain, "label": k["label"], "group": g["name"]})


# ---- search + stats ----------------------------------------------------------

@_handler
async def search(request):
    body = await _body(request)
    try:
        size, page = int(body.get("page_size", body.get("k", 10))), int(body.get("page", 1))
    except (TypeError, ValueError):
        raise BadRequest("page and page_size must be integers")
    return JSONResponse(await request.app.state.gateway.search(body.get("query", ""), size, page))


@_handler
async def stats(request):
    return JSONResponse(request.app.state.gateway.stats())


@_handler
async def stats_detail(request):
    try:
        days = int(request.query_params.get("days", 30))
    except ValueError:
        raise BadRequest("days must be a whole number of days")
    if not 1 <= days <= 365:
        raise BadRequest("days must be between 1 and 365")
    return JSONResponse(request.app.state.gateway.stats_detail(days))


@_handler
async def audit(request):
    """What the manager changed by itself, in words: tool names renamed because two servers share
    one (`renamed`, as it stands now) and the history of those renames and of group defaults that
    followed them (`events`, newest first)."""
    rows = request.app.state.db.execute(
        "SELECT ts, kind, message FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return JSONResponse({"renamed": request.app.state.gateway.renamed(),
                         "events": [dict(r) for r in rows]})


@_handler
async def key_adaptive(request):
    """One key's adaptive ranking: the tools it would list (picked) and the next in line, with
    their scores. Learned for every key; `listed` says whether its group is adaptive, i.e.
    whether the picks are really in its app's list."""
    k = _key_row(request)
    g = request.app.state.db.execute("SELECT * FROM groups WHERE id = ?", (k["group_id"],)).fetchone()
    return JSONResponse({"id": k["id"], "label": k["label"], "group": g["name"], "mode": g["mode"],
                         **request.app.state.gateway.adaptive_view(k["id"], g)})


async def portal(request):
    # no-store on purpose. FileResponse sends last-modified but no cache-control, so browsers
    # apply heuristic freshness and can serve a stale copy of the admin UI after a deploy --
    # which looks exactly like "my edit didn't take effect until I refreshed".
    return FileResponse(request.app.state.portal_file, headers={"cache-control": "no-store"})


ROUTES = [
    Route("/api/v1/groups", list_groups, methods=["GET"]),
    Route("/api/v1/groups", create_group, methods=["POST"]),
    Route("/api/v1/groups/{name}", update_group, methods=["PATCH", "DELETE"]),
    Route("/api/v1/upstreams", list_upstreams, methods=["GET"]),
    Route("/api/v1/upstreams", add_upstream, methods=["POST"]),
    Route("/api/v1/upstreams/{name}", update_upstream, methods=["PATCH", "DELETE"]),
    Route("/api/v1/upstreams/{name}/reconnect", reconnect_upstream, methods=["POST"]),
    Route("/api/v1/test-connection", test_connection, methods=["POST"]),
    Route("/api/v1/tools", list_tools, methods=["GET"]),
    Route("/api/v1/overrides", list_overrides, methods=["GET"]),
    Route("/api/v1/overrides", set_override, methods=["POST"]),
    Route("/api/v1/keys", list_keys, methods=["GET"]),
    Route("/api/v1/keys", create_key, methods=["POST"]),
    Route("/api/v1/keys/{id}", update_key, methods=["PATCH", "DELETE"]),
    Route("/api/v1/keys/{id}/revoke", revoke_key, methods=["POST"]),
    Route("/api/v1/keys/{id}/reissue", reissue_key, methods=["POST"]),
    Route("/api/v1/search", search, methods=["POST"]),
    Route("/api/v1/stats", stats, methods=["GET"]),
    Route("/api/v1/stats/detail", stats_detail, methods=["GET"]),
    Route("/api/v1/audit", audit, methods=["GET"]),
    Route("/api/v1/keys/{id}/adaptive", key_adaptive, methods=["GET"]),
    Route("/", portal, methods=["GET"]),
]
