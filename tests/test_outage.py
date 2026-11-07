"""A server going down, as the model experiences it -- and the client-test findings about it.

The rule: a model calling a tool on a down server is told that the server is down and that
the manager keeps trying -- never "no such tool", never a bare timeout. When the server is
back, the same call just works, with nothing for the client to change. And a slow server is
not a down one.

Uses spiketools.py over HTTP, on its own port, because these tests have to kill and restart
an upstream -- which is not something to do to the real ones.
"""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

import httpx2 as httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import ADMIN, ALPHA, API, BETA, DB, MCP

H = {"authorization": f"Bearer {ADMIN}"}
PORT = 8896
URL = f"http://127.0.0.1:{PORT}/mcp"
SPIKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spiketools.py")


def _start():
    p = subprocess.Popen([sys.executable, SPIKE, str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
            return p
        except OSError:
            time.sleep(0.2)
    p.kill()
    pytest.fail("spiketools did not start")


def _stop(p):
    p.kill()
    p.wait(timeout=10)


def _status():
    return {u["name"]: u for u in httpx.get(f"{API}/upstreams", headers=H).json()}["spike"]


def _until(pred, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.5)
    return False


@pytest.fixture
def spike(manager):
    proc = {"p": _start()}
    # a 3 s handshake limit on purpose: tool calls must not be held to it
    r = httpx.post(f"{API}/upstreams", headers=H, timeout=60,
                   json={"name": "spike", "url": URL, "timeout_ms": 3000,
                         "description": "Spike test server for notes"})
    assert r.status_code == 201 and r.json()["status"] == "connected", r.text
    yield proc
    if proc["p"].poll() is None:
        _stop(proc["p"])
    httpx.delete(f"{API}/upstreams/spike", headers=H, timeout=60)


class Conversation:
    """One MCP session, held open across the steps of a test."""

    def __init__(self, key):
        self.key = key

    async def __aenter__(self):
        self._tr = httpx.AsyncClient(headers={"authorization": f"Bearer {self.key}"}, timeout=120)
        self._cm = streamable_http_client(MCP, http_client=self._tr)
        r, w, *_ = await self._cm.__aenter__()
        self._s = ClientSession(r, w)
        await self._s.__aenter__()
        await self._s.initialize()
        await self._s.list_tools()
        return self

    async def __aexit__(self, *exc):
        await self._s.__aexit__(*exc)
        await self._cm.__aexit__(*exc)
        await self._tr.aclose()

    async def raw(self, name, arguments=None):
        return await self._s.call_tool("call_tool", {"name": name, "arguments": arguments or {}})

    async def call(self, name, arguments=None):
        r = await self.raw(name, arguments)
        return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))

    async def search(self, query):
        r = await self._s.call_tool("search_tools", {"query": query})
        return "\n".join(c.text for c in r.content if hasattr(c, "text"))

    async def tools(self):
        return {t.name: t for t in (await self._s.list_tools()).tools}


# ---- a down server ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_call_to_a_down_server_says_so_quickly(spike):
    async with Conversation(BETA) as c:
        err, text = await c.call("get_note", {"text": "hi"})
        assert not err and "note: hi" in text

        _stop(spike["p"])
        t0 = time.monotonic()
        err, text = await c.call("get_note", {"text": "hi"})
        took = time.monotonic() - t0
        assert err and "spike is down right now" in text and "keeps trying to reconnect" in text, text
        assert "no tool" not in text.lower(), "a down server's tool still exists"
        assert took < 12, f"took {took:.1f}s: a dead server must not cost the whole read timeout"

        t0 = time.monotonic()
        err, text = await c.call("get_note")
        assert err and "spike is down right now" in text and time.monotonic() - t0 < 8

        state = _status()
        assert state["status"] == "down" and state["error"], state
        assert state["retry_in"] is not None and state["retry_in"] <= 60, "a retry is always scheduled, within a minute"


@pytest.mark.asyncio
async def test_a_down_servers_tools_stay_listed_and_searchable(spike):
    """A client that lists during an outage and never re-lists must still have them when the
    server is back. And the model reading the list is told the server is down."""
    _stop(spike["p"])
    httpx.patch(f"{API}/upstreams/spike", headers=H, timeout=60, json={"timeout_ms": 2000})  # a fresh link
    assert _until(lambda: _status()["status"] == "down", 15)

    async with Conversation(ALPHA) as direct:
        assert "get_note" in await direct.tools(), "direct: still in the list while down"
    async with Conversation(BETA) as c:
        desc = (await c.tools())["search_tools"].description
        assert "spike: 9 tools, down right now (the manager keeps retrying)" in desc, desc
        found = await c.search("note")
        assert "get_note" in found and "down right now" in found, found

    con = sqlite3.connect(DB)
    saved = con.execute("SELECT tools FROM upstreams WHERE name = 'spike'").fetchone()[0]
    con.close()
    assert "get_note" in {t["name"] for t in json.loads(saved)}


@pytest.mark.asyncio
async def test_search_points_at_a_down_server_that_would_answer(spike):
    """Searching for what a down server does used to return unrelated tools that happened to
    clear the floor, and nothing about the server that would actually answer."""
    _stop(spike["p"])
    async with Conversation(BETA) as c:
        await c.call("get_note")                                    # the manager finds it down
        found = await c.search("spike test notes")
        assert "spike (Spike test server for notes) looks like the place for this" in found, found
        assert "down right now" in found


@pytest.mark.asyncio
async def test_back_up_means_it_just_works_and_every_conversation_is_told(spike):
    async with Conversation(BETA) as using, Conversation(BETA) as other:
        assert not (await using.call("get_note"))[0]

        _stop(spike["p"])
        err, text = await other.call("get_note")                    # someone finds it down
        assert err and "down right now" in text

        err, text = await using.call("list_tabs")                   # the rest are told, unasked
        assert "Heads-up: spike just went down" in text, text
        err, text = await using.call("list_tabs")
        assert "Heads-up" not in text, "once per change, not on every call"

        spike["p"] = _start()
        assert _until(lambda: _status()["status"] == "connected", 30), _status()
        err, text = await using.call("list_tabs")
        assert "spike is up again: its 9 tools work now" in text, text
        err, text = await using.call("get_note", {"text": "again"})
        assert not err and "note: again" in text, "nothing for the client to change"


@pytest.mark.asyncio
async def test_a_new_server_is_announced_to_a_conversation_already_running(manager):
    """The server list in a client's descriptions is frozen when it connects. A server added
    later used to be invisible unless the model happened to search for its tools."""
    proc = _start()
    try:
        async with Conversation(BETA) as c:
            httpx.post(f"{API}/upstreams", headers=H, timeout=60,
                       json={"name": "latecomer", "url": URL}).raise_for_status()
            err, text = await c.call("list_tabs")
            assert "New server: latecomer (9 tools)" in text, text
    finally:
        httpx.delete(f"{API}/upstreams/latecomer", headers=H, timeout=60)
        _stop(proc)


# ---- a slow server is not a down one -----------------------------------------------

@pytest.mark.asyncio
async def test_a_slow_call_on_a_busy_server_is_not_called_down(spike):
    """A call outlasting the 2 s liveness check and the server's 3 s handshake limit must simply
    finish: it used to be cancelled as 'stopped answering', or cut off at the 15 s limit."""
    async with Conversation(BETA) as c:
        err, text = await c.call("slow_note", {"seconds": 5})
        assert not err and "slow note after 5" in text, text
        assert _status()["status"] == "connected"


# ---- a server that restarts under a call ---------------------------------------------
# A restarted server takes connections again within seconds, so "is the host up" says yes --
# but every session it had is gone, and a call that was running on one went with it.

def _restart(spike):
    _stop(spike["p"])
    spike["p"] = _start()


@pytest.mark.asyncio
async def test_a_call_cut_off_by_a_restart_fails_fast_and_says_so(spike):
    """It used to wait out the whole 10-minute call limit. A call with an effect is not re-run:
    it may already have happened."""
    async with Conversation(BETA) as c:
        await c.call("get_note")                                    # this key's session is open
        call = asyncio.ensure_future(c.call("slow_write", {"value": "x", "seconds": 30}))
        await asyncio.sleep(1.5)
        _restart(spike)
        t0 = time.monotonic()
        err, text = await asyncio.wait_for(call, 40)
        assert time.monotonic() - t0 < 20, f"took {time.monotonic() - t0:.0f}s"
        assert err and "spike restarted while slow_write was running" in text, text
        assert "may or may not" in text and "Check before running it again" in text, text


@pytest.mark.asyncio
async def test_a_read_only_call_cut_off_by_a_restart_is_run_again(spike):
    """readOnlyHint is the server's own word that running it twice changes nothing."""
    async with Conversation(BETA) as c:
        await c.call("get_note")
        call = asyncio.ensure_future(c.call("slow_note", {"seconds": 6}))
        await asyncio.sleep(1.5)
        _restart(spike)
        err, text = await asyncio.wait_for(call, 40)
        assert not err and "slow note after 6" in text, text
        assert "spike restarted" in text and "is gone" in text, "the model must know its state there was reset"
        assert "shows spike as it is now, just restarted" in text, "and that the result reads a fresh server"


class Strander:
    """A TCP hop that can strand the connections already open through it: they stop delivering
    but never close -- what a client is left holding when a container is replaced, while new
    connections already reach the new one. On that, a call used to hang for its whole limit."""

    def __init__(self, port, target):
        self.port, self.target, self.live, self.stranded = port, target, set(), set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    def strand(self):
        self.stranded |= self.live

    async def _handle(self, reader, writer):
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self.target)
        except OSError:
            writer.close()
            return
        conn = (writer, up_w)
        self.live.add(conn)

        async def pump(r, w):
            try:
                while data := await r.read(65536):
                    if conn not in self.stranded:
                        w.write(data)
                        await w.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                if conn not in self.stranded:
                    w.close()
        await asyncio.gather(pump(reader, up_w), pump(up_r, writer))


@pytest.mark.asyncio
async def test_a_call_stranded_by_a_replaced_container_does_not_wait_out_the_limit(spike):
    hop = Strander(8897, PORT)
    await hop.start()
    async with httpx.AsyncClient(headers=H, timeout=60) as api:   # async: the hop runs on this loop
        r = await api.post(f"{API}/upstreams", json={"name": "stranded", "url": "http://127.0.0.1:8897/mcp",
                                                      "timeout_ms": 3000})
        assert r.json()["status"] == "connected", r.text
        try:
            async with Conversation(BETA) as c:
                await c.call("stranded__get_note")                   # this key's session, through the hop
                call = asyncio.ensure_future(c.call("stranded__slow_write", {"value": "y", "seconds": 60}))
                await asyncio.sleep(1.5)
                hop.strand()
                await asyncio.to_thread(_restart, spike)
                t0 = time.monotonic()
                err, text = await asyncio.wait_for(call, 90)
                assert time.monotonic() - t0 < 30, f"took {time.monotonic() - t0:.0f}s"
                assert err and "stranded restarted while stranded__slow_write was running" in text, text
        finally:
            await api.delete(f"{API}/upstreams/stranded")
            hop.server.close()


@pytest.mark.asyncio
async def test_a_restart_between_calls_runs_on_a_fresh_session_and_says_so(spike):
    """Nothing to retry for the call itself -- but what the server kept for this client is gone,
    and a model that assumes its tabs or jobs are still there goes wrong without being told."""
    async with Conversation(BETA) as c:
        await c.call("remember", {"value": "before"})
        _restart(spike)
        err, text = await c.call("recall")
        assert not err and "recalled nothing" in text, text
        assert "spike restarted" in text and "is gone" in text, text


# ---- what reaches the model -------------------------------------------------------------

@pytest.mark.asyncio
async def test_notes_survive_a_tool_with_structured_output(spike):
    """Clients that see structuredContent show the model only that, so a note in `content`
    vanished. Through call_tool the result is text: the JSON, then the note."""
    async with Conversation(BETA) as c:
        r = await c.raw("get_info")
        assert r.structured_content is None, "call_tool declares no outputSchema; no structured copy"
        text = "\n".join(x.text for x in r.content if hasattr(x, "text"))
        assert '"version": 2' in text and "[mcp-manager] You just used spike" in text, text


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_servers")      # the other server it names is the real browser
async def test_a_blocked_fetch_points_at_another_server(spike):
    """web_url_read blocked by a site, while the browser read the same page fine: the failure
    should say where the same job can be done."""
    async with Conversation(BETA) as c:
        err, text = await c.call("fetch_page", {"url": "https://example.com"})
        assert err and "blocked" in text
        assert "another server can do the same job" in text and "[browser]" in text, text


# ---- every client has its own session ----------------------------------------------------

async def _direct(key, name, arguments=None):
    """A direct-mode call (no call_tool there): the tool by its own name."""
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=60) as tr:
        async with streamable_http_client(MCP, http_client=tr) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(name, arguments or {})
                return "\n".join(c.text for c in res.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_two_keys_never_share_a_session(spike):
    """A server keeps per-client state per MCP session -- a browser's tabs and logins, a job
    list. Through one shared session every app was one client to it. Now each key has its own."""
    async with Conversation(BETA) as b:
        assert "remembered for-alpha" in await _direct(ALPHA, "remember", {"value": "for-alpha"})
        assert "remembered for-beta" in (await b.call("remember", {"value": "for-beta"}))[1]
        assert "recalled for-alpha" in await _direct(ALPHA, "recall"), "alpha saw someone else's state"
        assert "recalled for-beta" in (await b.call("recall"))[1], "beta saw someone else's state"
    assert _status()["sessions"] >= 2, _status()


@pytest.mark.asyncio
async def test_one_keys_conversations_share_its_session(spike):
    """The boundary is the key -- one app. Two chats of the same app are one client to the
    server, as they would be with one MCP config of its own."""
    async with Conversation(BETA) as one, Conversation(BETA) as two:
        await one.call("remember", {"value": "shared"})
        assert "recalled shared" in (await two.call("recall"))[1]


@pytest.mark.asyncio
async def test_a_server_shared_per_group_gives_the_whole_group_one_session(spike):
    """The admin's exception to isolation, per server: one person's devices -- several keys in
    one group -- meet the same state there. Other groups stay apart, flipping the switch back
    restores isolation, and revoking one device's key leaves the others' (the group's) session."""
    httpx.post(f"{API}/groups", headers=H, json={"name": "home", "mode": "hybrid"}).raise_for_status()
    phone = httpx.post(f"{API}/keys", headers=H, json={"group": "home", "label": "phone"}).json()
    laptop = httpx.post(f"{API}/keys", headers=H, json={"group": "home", "label": "laptop"}).json()
    try:
        async with Conversation(phone["key"]) as p, Conversation(laptop["key"]) as l:
            await p.call("remember", {"value": "isolated"})
            assert "recalled nothing" in (await l.call("recall"))[1], "two keys, two sessions: the rule"

        r = httpx.patch(f"{API}/upstreams/spike", headers=H, json={"shared": True}, timeout=60)
        assert r.status_code == 200 and _status()["shared"] is True
        async with Conversation(phone["key"]) as p, Conversation(laptop["key"]) as l, Conversation(BETA) as other:
            await p.call("remember", {"value": "for-home"})
            assert "recalled for-home" in (await l.call("recall"))[1], "the group shares one session"
            assert "recalled for-home" not in (await other.call("recall"))[1], "another group stays apart"
            httpx.post(f"{API}/keys/{phone['id']}/revoke", headers=H).raise_for_status()
            assert "recalled for-home" in (await l.call("recall"))[1], \
                "revoking the phone must not close the laptop's (the group's) session"

        httpx.patch(f"{API}/upstreams/spike", headers=H, json={"shared": False}, timeout=60).raise_for_status()
        async with Conversation(laptop["key"]) as l:
            assert "recalled for-home" not in (await l.call("recall"))[1], "switched off: own sessions again"
    finally:
        httpx.delete(f"{API}/groups/home", headers=H)


@pytest.mark.asyncio
async def test_reconnecting_a_server_starts_every_session_over(spike):
    """The portal's reconnect button: a fresh session for the manager (headers sent again, tools
    listed again) and for every app on it -- what the server kept for an app is gone after."""
    async with Conversation(BETA) as c:
        await c.call("remember", {"value": "before-reconnect"})
        assert "recalled before-reconnect" in (await c.call("recall"))[1]
        r = httpx.post(f"{API}/upstreams/spike/reconnect", headers=H, timeout=60)
        assert r.status_code == 200 and r.json()["status"] == "connected" and r.json()["tools"] > 0, r.text
        assert "recalled nothing" in (await c.call("recall"))[1], "the app's old session must be gone"
    httpx.patch(f"{API}/upstreams/spike", headers=H, json={"enabled": False}, timeout=60).raise_for_status()
    try:
        r = httpx.post(f"{API}/upstreams/spike/reconnect", headers=H, timeout=60)
        assert r.status_code == 400 and "turned off" in r.json()["error"]
    finally:
        httpx.patch(f"{API}/upstreams/spike", headers=H, json={"enabled": True}, timeout=60).raise_for_status()
    assert httpx.post(f"{API}/upstreams/nope/reconnect", headers=H).status_code == 404


@pytest.mark.asyncio
async def test_revoking_a_key_closes_its_sessions(spike):
    created = httpx.post(f"{API}/keys", headers=H, json={"group": "imagelibrary", "label": "short-lived"}).json()
    before = _status()["sessions"]
    async with Conversation(created["key"]) as c:
        await c.call("remember", {"value": "x"})
    assert _status()["sessions"] == before + 1
    httpx.post(f"{API}/keys/{created['id']}/revoke", headers=H).raise_for_status()
    assert _until(lambda: _status()["sessions"] == before, 15), _status()
