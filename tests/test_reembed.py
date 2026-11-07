"""An edit to anything a tool is matched on re-embeds it -- and only what changed.

Deterministic: the embedding model is replaced by a stub that records what it was asked to embed,
so this runs without the ~300 MB model.
"""

import pytest

from mcp_manager import db, scoring
from test_conversations import _gateway


@pytest.fixture
def gw(monkeypatch):
    gw, _ = _gateway()
    calls = []

    async def fake_embed(texts, cfg, kind="document"):
        calls.append(list(texts))
        return [[float(len(t)), 1.0, float(sum(map(ord, t)) % 97)] for t in texts]

    monkeypatch.setattr(scoring, "embed", fake_embed)
    monkeypatch.setattr(gw, "_can_embed", lambda: True)
    gw.calls = calls
    return gw


def _embedded(gw):
    return [t for batch in gw.calls for t in batch]


@pytest.mark.asyncio
async def test_a_description_or_title_edit_reembeds_that_tool_only(gw):
    await gw.reembed()
    assert gw.index()["mode"] == "semantic" and len(_embedded(gw)) == 3
    gw.calls.clear()
    db.set_override(gw.db, "browser", "navigate", "Open a web address", title="Go somewhere")
    await gw.reembed()
    done = _embedded(gw)
    assert len(done) == 1 and "Open a web address" in done[0] and "Go somewhere" in done[0], done
    assert gw.index()["mode"] == "semantic", "every tool has a vector again"


@pytest.mark.asyncio
async def test_a_server_rename_or_description_edit_reembeds_all_its_tools(gw):
    await gw.reembed()
    gw.calls.clear()
    gw.db.execute("UPDATE upstreams SET description = 'A real browser you can drive' WHERE name = 'browser'")
    await gw.reembed()
    done = _embedded(gw)
    assert len(done) == 3 and all("A real browser you can drive" in d for d in done), done


@pytest.mark.asyncio
async def test_an_unchanged_catalogue_is_not_embedded_again(gw):
    await gw.reembed()
    gw.calls.clear()
    await gw.reembed()
    assert _embedded(gw) == [], "vectors are cached by their text"
