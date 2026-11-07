"""A failing server must never take the MCP endpoint down -- deterministic, no network.

Every server's task lives in one task group, and that group is what keeps /mcp alive: an exception
escaping any of them ended the endpoint for every app, while the process and the admin panel looked
healthy. A stop (a server removed or edited) can land at the very moment one of its requests fails,
and then it is the request's error that surfaces, not the cancellation. Found by a fresh clone's test
run: a server deleted just as its session failed.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import mcp
import mcp.client.streamable_http
import pytest

from mcp_manager import db, gateway
from mcp_manager.gateway import ClientLink, Gateway, Link


class FakeSession:
    """Stands in for the SDK's session; `on_init` is what happens when it opens."""
    on_init = staticmethod(lambda: None)

    def __init__(self, *_, **__):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def initialize(self):
        FakeSession.on_init()
        return SimpleNamespace(instructions=None, server_info=None)

    async def list_tools(self):
        return SimpleNamespace(tools=[])


@asynccontextmanager
async def fake_streams(*_, **__):
    yield None, None


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", fake_streams)
    monkeypatch.setattr(gateway, "_auth_client", lambda headers: None)
    monkeypatch.setattr(FakeSession, "on_init", staticmethod(lambda: None))
    database = db.connect(":memory:")
    database.execute("INSERT INTO groups (name) VALUES ('g')")
    database.execute("INSERT INTO upstreams (name, url) VALUES ('s', 'http://127.0.0.1:1/mcp')")
    return Gateway({"default_group": "g"}, database, None, "")


def _link(gw):
    return Link(gw.db.execute("SELECT * FROM upstreams").fetchone())


def _stop_lands_on_a_failure():
    asyncio.current_task().cancel()              # the stop arrives...
    raise RuntimeError("Session not found")      # ...as the request fails: this is what surfaces


async def _in_the_shared_group(run):
    """The task, beside another server's: the other one must outlive it."""
    async with asyncio.TaskGroup() as tg:
        neighbour = tg.create_task(asyncio.sleep(0.05, "still here"))
        task = tg.create_task(run)
    return task, neighbour.result()


def test_a_stop_landing_on_a_failing_health_check_leaves_the_other_servers_alone(gw, monkeypatch):
    async def hold(link, session):
        _stop_lands_on_a_failure()
    monkeypatch.setattr(gw, "_hold", hold)
    _, neighbour = asyncio.run(_in_the_shared_group(gw._run(_link(gw))))
    assert neighbour == "still here"


def test_a_stop_landing_on_a_failing_client_session_leaves_the_other_servers_alone(gw, monkeypatch):
    monkeypatch.setattr(FakeSession, "on_init", staticmethod(_stop_lands_on_a_failure))
    client = ClientLink(1, _link(gw))
    gw._clients[(1, "s")] = client
    _, neighbour = asyncio.run(_in_the_shared_group(gw._run_client(client)))
    assert neighbour == "still here"
    assert (1, "s") not in gw._clients, "its place is cleared whichever way it ended"


def test_a_plain_stop_still_stops(gw, monkeypatch):
    async def hold(link, session):
        await asyncio.Event().wait()             # holding the connection, as it does all day
    monkeypatch.setattr(gw, "_hold", hold)

    async def scenario():
        task = asyncio.create_task(gw._run(_link(gw)))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.wait({task}, timeout=2)
        return task

    task = asyncio.run(scenario())
    assert task.done() and task.cancelled()


def test_a_failure_with_no_stop_is_recorded_and_retried(gw, monkeypatch):
    tries = []

    async def hold(link, session):
        tries.append(1)
        raise RuntimeError("Session not found")
    monkeypatch.setattr(gw, "_hold", hold)
    link = _link(gw)

    async def scenario():
        task = asyncio.create_task(gw._run(link))
        await asyncio.sleep(0.05)
        link.wake.set()                          # a call that needs it cuts the wait short
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.wait({task}, timeout=2)

    asyncio.run(scenario())
    assert len(tries) >= 2, "it reconnects: an ordinary failure is not a stop"
    assert "Session not found" in link.error and link.session is None
