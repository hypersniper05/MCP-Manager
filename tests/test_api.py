"""The management API — the surface a settings page is built on (README, "Management API").

These are not decoration: if any of these break, the settings page cannot exist. The
override test is the one that matters most, because a description edit that stores but
never reaches search is a feature that silently does nothing.
"""

import hashlib

import pytest
import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import API, ADMIN, MCP, ALPHA, BETA, UPSTREAMS
from mcp_manager.api import ROUTES
from mcp_manager.db import now

H = {"authorization": f"Bearer {ADMIN}"}
NATIVE = {"search_tools", "call_tool", "list_mcpservers", "get_tools_from_server"}
YTDL = UPSTREAMS.get("ytdl")
pytestmark = pytest.mark.usefixtures("real_servers")


async def sees(key):
    """What a key actually gets, through a real client — not a raw POST at a
    session-based endpoint, which just hangs and reads as a timeout."""
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=30) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}


async def instructions(key):
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=30) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                return (await s.initialize()).instructions or ""


def new_key(group, label):
    r = httpx.post(f"{API}/keys", headers=H, json={"group": group, "label": label})
    assert r.status_code == 201, r.text
    return r.json()


# ---- the boundary ---------------------------------------------------------------

def test_every_api_route_refuses_without_the_admin_token(manager):
    """One check, in the gate, for every path under /api/ -- so walk every route rather than
    trust that each handler remembered. An MCP key is not an admin token either: the settings
    surface is not reachable from a model."""
    for route in ROUTES:
        if not route.path.startswith("/api/"):
            continue
        path = route.path.replace("{name}", "full").replace("{id}", "1")
        for method in route.methods - {"HEAD"}:
            for headers in ({}, {"authorization": f"Bearer {ALPHA}"}):
                r = httpx.request(method, f"{API[:-len('/api/v1')]}{path}", headers=headers, json={})
                assert r.status_code == 401, f"{method} {path} with {headers or 'nothing'}: {r.status_code}"


def test_a_bad_body_is_a_400_not_a_crash(manager):
    r = httpx.post(f"{API}/groups", headers={**H, "content-type": "application/json"}, content=b"{nope")
    assert r.status_code == 400 and "JSON" in r.json()["error"]


# ---- groups -----------------------------------------------------------------------

def test_groups_report_their_live_view(manager):
    groups = {g["name"]: g for g in httpx.get(f"{API}/groups", headers=H).json()}
    assert groups["full"]["mode"] == "direct" and groups["full"]["sees"] > 50
    assert groups["imagelibrary"]["mode"] == "hybrid" and groups["imagelibrary"]["sees"] == 3


def test_only_the_three_modes_exist(manager):
    for mode in ("manual", "reveal", "semantic", ""):
        r = httpx.post(f"{API}/groups", headers=H, json={"name": f"bad_{mode}", "mode": mode})
        assert r.status_code == 400, mode
    assert "adaptive" in r.json()["error"]


@pytest.mark.asyncio
async def test_an_adaptive_key_gets_what_it_uses_into_its_list(manager):
    """Starts with nothing learned; two calls to a tool and it is in that key's list; the
    ranking and the statistics page both say so."""
    httpx.post(f"{API}/groups", headers=H, json={"name": "learns", "mode": "adaptive"}).raise_for_status()
    k = new_key("learns", "learner")
    assert await sees(k["key"]) == NATIVE, "a new key starts with nothing learned"
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {k['key']}"}, timeout=60) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for _ in range(2):
                    res = await s.call_tool("call_tool", {"name": "server_status", "arguments": {}})
                    assert not res.is_error
    assert await sees(k["key"]) == NATIVE | {"server_status"}

    a = httpx.get(f"{API}/keys/{k['id']}/adaptive", headers=H).json()
    assert a["mode"] == "adaptive" and a["listed"] is True
    assert a["tools"][0]["tool"] == "server_status" and a["tools"][0]["picked"] is True

    d = httpx.get(f"{API}/stats/detail?days=7", headers=H).json()
    assert {"calls", "status", "p50_ms", "tools", "servers", "apps", "heat", "chains"} <= set(d)
    app = next(x for x in d["apps"] if x["label"] == "learner")
    assert app["calls"] == 2 and app["mode"] == "adaptive" and app["adaptive"][0]["picked"]
    assert httpx.get(f"{API}/stats/detail?days=0", headers=H).status_code == 400


@pytest.mark.asyncio
async def test_switching_mode_changes_what_the_key_sees(manager):
    httpx.post(f"{API}/groups", headers=H,
               json={"name": "switch", "mode": "hybrid", "defaults": ["screenshot"]}).raise_for_status()
    key = new_key("switch", "switch")["key"]
    assert await sees(key) == {"screenshot"} | NATIVE

    httpx.patch(f"{API}/groups/switch", headers=H, json={"mode": "direct"}).raise_for_status()
    seen = await sees(key)
    assert len(seen) > 50 and not seen & NATIVE

    httpx.patch(f"{API}/groups/switch", headers=H,
                json={"mode": "hybrid", "defaults": ["navigate", "screenshot"]}).raise_for_status()
    assert await sees(key) == {"navigate", "screenshot"} | NATIVE


@pytest.mark.asyncio
async def test_a_group_reaches_only_the_servers_it_chose(manager):
    r = httpx.post(f"{API}/groups", headers=H, json={"name": "narrow", "mode": "direct"})
    assert r.status_code == 201 and r.json()["servers"] is None, "all of them, until told otherwise"
    everything = r.json()["sees"]
    key = httpx.post(f"{API}/keys", headers=H, json={"group": "narrow", "label": "narrow app"}).json()["key"]
    try:
        for bad in ("imagegen", [1], ["no such server"]):
            r = httpx.patch(f"{API}/groups/narrow", headers=H, json={"servers": bad})
            assert r.status_code == 400, bad
        r = httpx.patch(f"{API}/groups/narrow", headers=H, json={"servers": ["imagegen"]})
        assert r.status_code == 200 and r.json()["servers"] == ["imagegen"]
        tools = {t["exposed"]: t["upstream"] for t in httpx.get(f"{API}/tools", headers=H).json()}
        listed = await sees(key)
        assert listed and {tools[n] for n in listed} == {"imagegen"}
        assert r.json()["sees"] == len(listed) < everything
        r = httpx.patch(f"{API}/groups/narrow", headers=H, json={"servers": []})
        assert r.json()["servers"] == [] and r.json()["sees"] == 0 and not await sees(key)
        r = httpx.patch(f"{API}/groups/narrow", headers=H, json={"servers": None})
        assert r.json()["servers"] is None and r.json()["sees"] == everything
    finally:
        httpx.delete(f"{API}/groups/narrow", headers=H)


def test_a_new_group_can_start_with_its_servers_and_a_removed_one_leaves(manager):
    httpx.post(f"{API}/upstreams", headers=H, timeout=60,
               json={"name": "brief", "url": "http://127.0.0.1:1/mcp"}).raise_for_status()
    r = httpx.post(f"{API}/groups", headers=H, json={"name": "chosen", "servers": ["brief", "imagegen"]})
    assert r.status_code == 201 and r.json()["servers"] == ["brief", "imagegen"]
    httpx.patch(f"{API}/upstreams/brief", headers=H, timeout=60, json={"name": "renamed"}).raise_for_status()
    group = next(g for g in httpx.get(f"{API}/groups", headers=H).json() if g["name"] == "chosen")
    assert group["servers"] == ["imagegen", "renamed"]
    httpx.delete(f"{API}/upstreams/renamed", headers=H, timeout=60).raise_for_status()
    group = next(g for g in httpx.get(f"{API}/groups", headers=H).json() if g["name"] == "chosen")
    assert group["servers"] == ["imagegen"]
    httpx.delete(f"{API}/groups/chosen", headers=H)


def test_tools_carry_the_servers_own_words_through_an_edit_and_back(manager):
    tool = httpx.get(f"{API}/tools", headers=H).json()[0]
    own = tool["server_says"]
    assert tool["description"] == own and not tool["overridden"]
    body = {"upstream": tool["upstream"], "tool": tool["tool"]}
    httpx.post(f"{API}/overrides", headers=H, timeout=90, json={**body, "description": "mine"}).raise_for_status()
    edited = next(t for t in httpx.get(f"{API}/tools", headers=H).json() if t["tool"] == tool["tool"])
    assert edited["description"] == "mine" and edited["server_says"] == own and edited["overridden"]
    httpx.post(f"{API}/overrides", headers=H, timeout=90, json={**body, "description": None}).raise_for_status()
    back = next(t for t in httpx.get(f"{API}/tools", headers=H).json() if t["tool"] == tool["tool"])
    assert back["description"] == own and not back["overridden"]


def test_an_adaptive_group_chooses_how_many_learned_tools_it_lists(manager):
    r = httpx.post(f"{API}/groups", headers=H, json={"name": "capped", "mode": "adaptive"})
    assert r.status_code == 201 and r.json()["adaptive_cap"] == 10, "10, the measured default"
    for bad in (0, 51, "20", 2.5, True):
        r = httpx.patch(f"{API}/groups/capped", headers=H, json={"adaptive_cap": bad})
        assert r.status_code == 400 and "1 to 50" in r.json()["error"], bad
    assert httpx.patch(f"{API}/groups/capped", headers=H, json={"adaptive_cap": 50}).json()["adaptive_cap"] == 50
    assert httpx.patch(f"{API}/groups/capped", headers=H, json={"adaptive_cap": None}).json()["adaptive_cap"] == 10
    httpx.delete(f"{API}/groups/capped", headers=H)


def test_a_renamed_group_keeps_its_keys_and_the_audit_says_so(manager):
    httpx.post(f"{API}/groups", headers=H, json={"name": "before", "mode": "hybrid"}).raise_for_status()
    k = new_key("before", "renamed-group-key")
    r = httpx.patch(f"{API}/groups/before", headers=H, json={"name": "after-rename"})
    assert r.status_code == 200 and r.json()["name"] == "after-rename"
    keys = {x["label"]: x for x in httpx.get(f"{API}/keys", headers=H).json()}
    assert keys["renamed-group-key"]["group_name"] == "after-rename", "the key follows by id"
    audit = httpx.get(f"{API}/audit", headers=H).json()
    assert {"renamed", "events"} <= set(audit)
    assert any('Group "before" was renamed to "after-rename"' in e["message"] for e in audit["events"])
    httpx.delete(f"{API}/keys/{k['id']}", headers=H)


def test_a_default_that_names_no_tool_is_flagged(manager):
    """A settings page has to be able to say 'this default is gone' -- a server down, or a
    tool renamed upstream -- instead of quietly showing one tool fewer."""
    g = httpx.post(f"{API}/groups", headers=H,
                   json={"name": "stale", "defaults": ["screenshot", "no_such_tool"]}).json()
    assert g["unresolved"] == ["no_such_tool"] and g["sees"] == 1


@pytest.mark.asyncio
async def test_renaming_a_group_keeps_its_keys(manager):
    httpx.post(f"{API}/groups", headers=H, json={"name": "before", "defaults": ["screenshot"]}).raise_for_status()
    key = new_key("before", "renamed")["key"]
    r = httpx.patch(f"{API}/groups/before", headers=H, json={"name": "after"})
    assert r.status_code == 200 and r.json()["name"] == "after"
    assert await sees(key) == {"screenshot"} | NATIVE


@pytest.mark.asyncio
async def test_deleting_a_group_takes_its_keys_with_it(manager):
    """A key pointing at a group that no longer exists cannot resolve to a view -- it would
    connect and quietly see nothing. So the keys go with the group, and the response says how
    many went. Without a token, nothing gets deleted."""
    httpx.post(f"{API}/groups", headers=H, json={"name": "ui_delete"}).raise_for_status()
    created = new_key("ui_delete", "doomed")
    assert await sees(created["key"]), "the key works while its group exists"
    assert httpx.delete(f"{API}/groups/ui_delete").status_code == 401, "still needs the token"

    r = httpx.delete(f"{API}/groups/ui_delete", headers=H).json()
    assert r.get("ok") and r.get("deleted_keys") == 1, r
    assert "ui_delete" not in [g["name"] for g in httpx.get(f"{API}/groups", headers=H).json()]
    with pytest.raises(Exception):
        await sees(created["key"])


# ---- what the model is told ------------------------------------------------------

@pytest.mark.asyncio
async def test_hybrid_instructions_index_the_servers_and_say_how_to_reach_them(manager):
    """A hybrid client sees a handful of tools; this text is what says the rest exists, and
    the one way to reach it. Asserted as facts, not as exact wording."""
    text = (await instructions(BETA)).lower()
    for fact in ("search_tools", "call_tool", "arguments", "closest match", "first time you use a server"):
        assert fact in text, f"instructions no longer carry: {fact!r}"
    counts = {}
    for t in httpx.get(f"{API}/tools", headers=H).json():
        counts[t["upstream"]] = counts.get(t["upstream"], 0) + 1
    for name, count in counts.items():
        assert f"{name}: {count} tools" in text, name


@pytest.mark.asyncio
async def test_direct_instructions_do_not_mention_search(manager):
    text = (await instructions(ALPHA)).lower()
    assert "every tool" in text and "search_tools" not in text


# ---- servers -----------------------------------------------------------------------

def test_upstreams_report_status_and_what_the_model_is_told(manager):
    ups = {u["name"]: u for u in httpx.get(f"{API}/upstreams", headers=H).json()}
    assert len(ups) >= 3 and all(u["status"] == "connected" for u in ups.values()), ups
    u = list(ups.values())[0]
    assert {"description", "overridden", "server_says", "error", "tools", "enabled"} <= set(u)
    assert u["description"], "every server should say something about itself"


@pytest.mark.asyncio
async def test_a_dead_server_is_down_and_says_why_and_the_rest_keep_serving(manager):
    r = httpx.post(f"{API}/upstreams", headers=H, timeout=60,
                   json={"name": "gone", "url": "http://127.0.0.1:1/mcp"})
    assert r.status_code == 201, r.text
    try:
        assert r.json()["status"] == "down" and r.json()["error"], r.json()
        assert len(await sees(ALPHA)) > 50, "one dead server must not cost anyone else"
    finally:
        assert httpx.delete(f"{API}/upstreams/gone", headers=H, timeout=60).json().get("ok")
    assert "gone" not in {u["name"] for u in httpx.get(f"{API}/upstreams", headers=H).json()}


def test_editing_a_servers_url_reconnects_it(manager):
    """Edit used to store the new url and keep talking to the old one until a restart."""
    httpx.post(f"{API}/upstreams", headers=H, timeout=60,
               json={"name": "mirror", "url": "http://127.0.0.1:1/mcp"}).raise_for_status()
    try:
        r = httpx.patch(f"{API}/upstreams/mirror", headers=H, timeout=60, json={"url": YTDL})
        assert r.status_code == 200 and r.json()["status"] == "connected", r.json()
        assert r.json()["tools"] > 0
        r = httpx.patch(f"{API}/upstreams/mirror", headers=H, timeout=60, json={"enabled": False})
        assert r.json()["status"] == "disabled" and r.json()["tools"] == 0
    finally:
        httpx.delete(f"{API}/upstreams/mirror", headers=H, timeout=60)


def test_a_duplicate_server_name_is_a_conflict_not_an_overwrite(manager):
    r = httpx.post(f"{API}/upstreams", headers=H, json={"name": "browser", "url": "http://127.0.0.1:1/mcp"})
    assert r.status_code == 409
    # said the way the admin page shows it to a person, not as SQLite's constraint text
    assert r.json()["error"] == "name taken: there is already a server with that name"
    assert httpx.post(f"{API}/upstreams", headers=H, json={"name": "x", "url": "ftp://nope"}).status_code == 400
    assert httpx.post(f"{API}/upstreams", headers=H, json={"name": "x"}).status_code == 400


def test_a_server_can_carry_auth_headers(manager):
    """Servers behind auth are ordinary on a network. The headers must be stored, listed back
    whole (the portal pre-fills its editor from that), replaceable, clearable -- and refused
    when malformed rather than half-applied."""
    httpx.post(f"{API}/upstreams", headers=H, timeout=60,
               json={"name": "authy", "url": "http://127.0.0.1:1/mcp",
                     "headers": {"Authorization": "Bearer secret-token"}}).raise_for_status()
    try:
        got = next(u for u in httpx.get(f"{API}/upstreams", headers=H).json() if u["name"] == "authy")
        assert got["headers"] == {"Authorization": "Bearer secret-token"}, got
        r = httpx.patch(f"{API}/upstreams/authy", headers=H, timeout=60,
                        json={"headers": {"X-Key": "abc", "Authorization": "y"}})
        assert r.json()["headers"] == {"X-Key": "abc", "Authorization": "y"}, r.json()
        assert httpx.patch(f"{API}/upstreams/authy", headers=H, timeout=60,
                           json={"headers": {}}).json()["headers"] == {}
        for bad in (["Authorization=x"], {"No": 42}, {"": "v"}):
            assert httpx.patch(f"{API}/upstreams/authy", headers=H,
                               json={"headers": bad}).status_code == 400, bad
    finally:
        httpx.delete(f"{API}/upstreams/authy", headers=H, timeout=60)
    # live proof a REGISTERED server gets them: register the test manager itself, which
    # admits nothing without a key -- with ALPHA's in the headers it must connect
    httpx.post(f"{API}/upstreams", headers=H, timeout=60,
               json={"name": "self", "url": MCP,
                     "headers": {"Authorization": f"Bearer {ALPHA}"}}).raise_for_status()
    try:
        got = next(u for u in httpx.get(f"{API}/upstreams", headers=H).json() if u["name"] == "self")
        assert got["status"] == "connected" and got["tools"] >= 4, got
    finally:
        httpx.delete(f"{API}/upstreams/self", headers=H, timeout=60)


def test_test_connection_probes_an_endpoint_without_saving_it(manager):
    """The portal's Test connection button: the answer must be the truth about that URL --
    tools for a live one, a plain error for a dead one -- and nothing may survive the call:
    no server row, no background link, no key touched."""
    before = len(httpx.get(f"{API}/upstreams", headers=H).json())
    r = httpx.post(f"{API}/test-connection", headers=H, timeout=60, json={"url": YTDL})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["tools"] > 0, r.json()
    r = httpx.post(f"{API}/test-connection", headers=H, timeout=60,
                   json={"url": "http://127.0.0.1:1/mcp", "headers": {"Authorization": "Bearer x"}})
    assert r.status_code == 200 and not r.json()["ok"] and r.json()["error"], r.json()
    # proof the header actually travels: the test manager's own endpoint, which admits
    # nothing without a key — and refuses the same URL with the header left out
    r = httpx.post(f"{API}/test-connection", headers=H, timeout=60,
                   json={"url": MCP, "headers": {"Authorization": f"Bearer {ALPHA}"}})
    assert r.json()["ok"] and r.json()["tools"] >= 4, r.json()
    assert not httpx.post(f"{API}/test-connection", headers=H, timeout=60,
                          json={"url": MCP}).json()["ok"], "without a key the endpoint must refuse"
    assert httpx.post(f"{API}/test-connection", headers=H, json={"url": "ftp://no"}).status_code == 400
    assert len(httpx.get(f"{API}/upstreams", headers=H).json()) == before


# ---- tools and overrides -------------------------------------------------------------

def test_editing_a_description_moves_search_and_clearing_it_reverts(manager):
    """The loop the feature promises: edit text -> search finds it. Blank -> the server's
    own words again, and no override left behind."""
    query = "frobninate the widget"
    hits = lambda: [h["tool"] for h in httpx.post(f"{API}/search", headers=H,
                                                  json={"query": query, "k": 10}).json()["results"]]
    assert "list_frames" not in hits(), "test assumes this tool does not already match"

    r = httpx.post(f"{API}/overrides", headers=H,
                   json={"upstream": "browser", "tool": "list_frames",
                         "description": "frobninate the widget before anything else"})
    assert r.status_code == 200 and r.json().get("reindex")
    assert "list_frames" in hits()

    httpx.post(f"{API}/overrides", headers=H, json={"upstream": "browser", "tool": "list_frames",
                                                     "description": ""})
    assert "list_frames" not in hits()
    assert not [o for o in httpx.get(f"{API}/overrides", headers=H).json() if o["tool"] == "list_frames"]


def test_tools_endpoint_lists_what_the_picker_needs(manager):
    tools = httpx.get(f"{API}/tools", headers=H).json()
    assert len(tools) > 50
    row = [t for t in tools if t["exposed"] == "screenshot"][0]
    assert set(row) == {"exposed", "upstream", "tool", "description", "overridden", "server_says", "arguments"}


# ---- keys ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_generated_key_works_and_is_never_shown_twice(manager):
    created = new_key("imagelibrary", "throwaway")
    assert created["key"].startswith("mcpm_")
    row = [k for k in httpx.get(f"{API}/keys", headers=H).json() if k["id"] == created["id"]][0]
    assert "key" not in row, "the listing must not leak the secret back"
    # the hint is a hash prefix, not a key prefix: enough to recognise a row in the UI,
    # not enough to rebuild the secret from a screenshot of it
    assert row["hint"] == hashlib.sha256(created["key"].encode()).hexdigest()[:8]
    assert await sees(created["key"]) == {"server_status", "generate_image", "list_images"} | NATIVE


def test_an_unlabelled_key_is_not_labelled_with_its_own_secret(manager):
    """The label used to default to the plain key -- stored in the DB and listed back on every
    GET, which made the 'shown once' promise false."""
    r = httpx.post(f"{API}/keys", headers=H, json={"group": "imagelibrary"}).json()
    assert r["key"] not in r["label"]
    assert all(r["key"] not in (k["label"] or "") for k in httpx.get(f"{API}/keys", headers=H).json())


@pytest.mark.asyncio
async def test_a_key_moves_between_groups_without_being_reissued(manager):
    """Re-issuing a key means reconfiguring the app that holds it; moving it does not."""
    created = new_key("imagelibrary", "mover")
    httpx.patch(f"{API}/keys/{created['id']}", headers=H, json={"group": "full"}).raise_for_status()
    assert len(await sees(created["key"])) > 50
    assert httpx.patch(f"{API}/keys/{created['id']}", headers=H, json={"group": "nope"}).status_code == 404


@pytest.mark.asyncio
async def test_using_a_key_stamps_last_used(manager):
    created = new_key("imagelibrary", "stamped")
    await sees(created["key"])
    row = [k for k in httpx.get(f"{API}/keys", headers=H).json() if k["id"] == created["id"]][0]
    assert row["last_used_at"], "the settings page shows when an app last connected"


@pytest.mark.asyncio
async def test_revoking_a_key_stops_it_immediately_and_delete_removes_it(manager):
    created = new_key("imagelibrary", "temp")
    assert await sees(created["key"])
    httpx.post(f"{API}/keys/{created['id']}/revoke", headers=H).raise_for_status()
    with pytest.raises(Exception):
        await sees(created["key"])
    assert httpx.delete(f"{API}/keys/{created['id']}", headers=H).json()["ok"]
    assert created["id"] not in [k["id"] for k in httpx.get(f"{API}/keys", headers=H).json()]
    assert httpx.post(f"{API}/keys/999999/revoke", headers=H).status_code == 404


@pytest.mark.asyncio
async def test_reissuing_a_key_mints_a_new_secret_for_the_same_row(manager):
    """The secret is shown once; when it is lost the row is re-issued rather than deleted and
    re-created: the label and the group stay, the old secret dies, a revoked key comes back."""
    created = new_key("imagelibrary", "reissued")
    assert await sees(created["key"])
    r = httpx.post(f"{API}/keys/{created['id']}/reissue", headers=H)
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["key"].startswith("mcpm_") and fresh["key"] != created["key"]
    assert fresh["label"] == "reissued" and fresh["group"] == "imagelibrary"
    with pytest.raises(Exception):
        await sees(created["key"])        # the old secret is refused from here on
    assert await sees(fresh["key"]) == {"server_status", "generate_image", "list_images"} | NATIVE
    httpx.post(f"{API}/keys/{created['id']}/revoke", headers=H).raise_for_status()
    again = httpx.post(f"{API}/keys/{created['id']}/reissue", headers=H).json()
    assert await sees(again["key"]), "re-issue is also the way a revoked key becomes live again"
    assert httpx.post(f"{API}/keys/999999/reissue", headers=H).status_code == 404


def test_a_key_label_can_be_renamed(manager):
    """The settings page renames without re-issuing: the app keeps its key."""
    created = new_key("imagelibrary", "before")
    httpx.patch(f"{API}/keys/{created['id']}", headers=H, json={"label": "after"}).raise_for_status()
    row = [k for k in httpx.get(f"{API}/keys", headers=H).json() if k["id"] == created["id"]][0]
    assert row["label"] == "after"
    assert row["hint"] == hashlib.sha256(created["key"].encode()).hexdigest()[:8], "renaming keeps the secret"


# ---- stats ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_answers_tools_and_a_daily_series(manager):
    """The overview panel's data: the most-used tools, and per-day counts for the volume
    chart. One real call must show up in both halves. Days with no calls stay absent --
    the page draws the line and fills the gaps, so a quiet manager stays a small answer."""
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {BETA}"}, timeout=60) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                assert not (await s.call_tool("server_status", {})).is_error

    st = httpx.get(f"{API}/stats", headers=H).json()
    assert isinstance(st, dict) and {"tools", "days"} <= set(st)
    hit = [t for t in st["tools"] if t["tool"] == "server_status"]
    assert hit and hit[0]["calls"] >= 1 and hit[0]["last_seen"], "the call must be counted"
    today = now()[:10]                       # UTC date -- the same clock events are written on
    day = [d for d in st["days"] if d["day"] == today]
    assert day and day[0]["calls"] >= 1, "today's calls must be in today's bucket"
    assert st["days"] == sorted(st["days"], key=lambda d: d["day"]), "the chart reads them in order"


# ---- transport -------------------------------------------------------------------------

def test_mcp_endpoint_answers_a_non_localhost_host(manager):
    """Binding 0.0.0.0 is decoration if the transport layer still refuses any Host header
    that is not localhost -- which is the SDK default. 421 = Misdirected Request."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    for host in ("100.64.0.5:8898", "192.168.1.5:8898", "mcp.internal:8898"):
        r = httpx.post(MCP, headers={"authorization": f"Bearer {ALPHA}", "host": host},
                       json=body, timeout=20)
        assert r.status_code == 200, f"refused over the network via Host: {host}: {r.status_code}"


def test_a_standalone_settings_page_can_call_the_api(manager):
    """A settings page in another app runs on another origin, which means the browser sends a
    CORS preflight -- and a preflight carries no Authorization header. If the gate 401s the
    preflight the real call never happens, so CORS has to sit in front of it. The call itself
    is still refused without the token."""
    pre = httpx.options(f"{API}/groups", headers={"origin": "http://example.invalid",
                                                  "access-control-request-method": "GET",
                                                  "access-control-request-headers": "authorization"})
    assert pre.status_code == 200, pre.text
    assert "*" in pre.headers.get("access-control-allow-origin", "")
    assert httpx.get(f"{API}/groups", headers={"origin": "http://example.invalid"}).status_code == 401
    assert httpx.get(f"{API}/groups", headers={"origin": "http://example.invalid",
                                               "authorization": f"Bearer {ADMIN}"}).status_code == 200
