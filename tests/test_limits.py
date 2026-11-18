"""The edge layer on its own: rate ceiling, lockout, body cap, headers, and who a caller is.

No network and no manager process -- the middleware is an ASGI callable, so a hand-built
scope/receive/send drives it directly and deterministically."""

import pytest

from mcp_manager.limits import Guard, Limits, client_addr, _is_local


def _scope(path="/mcp", peer=("8.8.8.8", 4321), headers=None, scheme="http"):
    hs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": "http", "path": path, "client": list(peer), "headers": hs, "scheme": scheme}


async def _call(mw, scope, body=b"", declare=True, chunk=1 << 16):
    """Drive one request through `mw` and return (status, headers_dict, bytes_read_by_app).
    The body is delivered in `chunk`-sized pieces, as a real server would, so the streaming
    body cap is exercised the way it actually runs."""
    chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)] if body else []
    read = {"n": 0}

    async def receive():
        if chunks:
            piece = chunks.pop(0)
            return {"type": "http.request", "body": piece, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    out = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out.setdefault("body", b"")

    async def app(scope, rcv, snd):
        while True:
            m = await rcv()
            if m["type"] != "http.request":
                break
            read["n"] += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await snd({"type": "http.response.start", "status": 200,
                   "headers": [(b"content-type", b"application/json")]})
        await snd({"type": "http.response.body", "body": b"{}"})

    if declare and body:
        scope["headers"].append((b"content-length", str(len(body)).encode()))
    mw.app = app
    await mw(scope, receive, send)
    return out.get("status"), out.get("headers", {}), read["n"]


def _mw(guard, *, trust=False, max_body=1 << 20, headers=True, https=False):
    return Limits(None, guard, trust_forwarded_for=trust,
                  max_body_bytes=max_body, security_headers=headers, https=https)


def _guard(**kw):
    base = {"rate_per_minute": 1000, "max_auth_failures": 8, "auth_window_seconds": 300,
            "ban_seconds": 300, "max_ban_seconds": 86400}
    base.update(kw)
    return Guard(base)


# -- who the caller is ------------------------------------------------------------------

def test_the_address_is_the_proxy_hop_only_when_trusted():
    s = _scope(peer=("127.0.0.1", 1), headers={"x-forwarded-for": "9.9.9.9"})
    assert client_addr(s, trust_forwarded_for=True) == "9.9.9.9"
    assert client_addr(s, trust_forwarded_for=False) == "127.0.0.1"


def test_a_forwarded_for_chain_takes_the_last_hop():
    s = _scope(headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
    assert client_addr(s, trust_forwarded_for=True) == "3.3.3.3"


def test_local_is_loopback_without_a_proxy_in_front():
    assert _is_local(_scope(peer=("127.0.0.1", 1)), False)
    assert not _is_local(_scope(peer=("10.0.0.5", 1)), False)
    # loopback but proxied -> not the operator
    assert not _is_local(_scope(peer=("127.0.0.1", 1), headers={"x-forwarded-for": "9.9.9.9"}), True)
    assert not _is_local(_scope(peer=("127.0.0.1", 1), headers={"tailscale-funnel-request": "?1"}), False)


def test_behind_a_trusted_proxy_an_unstamped_request_is_the_operator():
    # In Docker the peer is the bridge gateway, never loopback. With the proxy trusted, a
    # request it did not stamp (no X-Forwarded-For) reached the localhost port directly.
    assert _is_local(_scope(peer=("172.17.0.1", 9)), True)
    assert not _is_local(_scope(peer=("172.17.0.1", 9), headers={"x-forwarded-for": "9.9.9.9"}), True)


# -- rate ceiling -----------------------------------------------------------------------

async def test_rate_limit_blocks_after_the_ceiling():
    mw = _mw(_guard(rate_per_minute=3))
    for _ in range(3):
        status, _, _ = await _call(mw, _scope())
        assert status == 200
    status, headers, _ = await _call(mw, _scope())
    assert status == 429 and "retry-after" in headers


async def test_rate_zero_disables_the_ceiling():
    mw = _mw(_guard(rate_per_minute=0))
    for _ in range(50):
        status, _, _ = await _call(mw, _scope())
        assert status == 200


# -- lockout ----------------------------------------------------------------------------

def test_a_lockout_escalates_and_a_good_credential_lifts_it():
    g = _guard(max_auth_failures=3, ban_seconds=10, max_ban_seconds=100)
    for _ in range(3):
        g.fail("4.4.4.4")
    first = g.banned("4.4.4.4")
    assert 0 < first <= 10
    # a repeat offender's next ban is longer
    for _ in range(3):
        g.fail("4.4.4.4")
    assert g.banned("4.4.4.4") > first
    g.ok("4.4.4.4")
    assert g.banned("4.4.4.4") == 0.0


async def test_a_banned_caller_is_turned_away():
    g = _guard(max_auth_failures=2, ban_seconds=60)
    g.fail("5.5.5.5"); g.fail("5.5.5.5")
    status, headers, _ = await _call(_mw(g), _scope(peer=("5.5.5.5", 1)))
    assert status == 429 and "retry-after" in headers


async def test_the_operator_on_localhost_is_never_locked_out():
    g = _guard(max_auth_failures=1, ban_seconds=60)
    g.fail("127.0.0.1")
    status, _, _ = await _call(_mw(g), _scope(peer=("127.0.0.1", 1)))
    assert status == 200


# -- body cap ---------------------------------------------------------------------------

async def test_an_oversized_declared_body_is_refused_up_front():
    status, _, read = await _call(_mw(_guard(), max_body=100), _scope(), body=b"x" * 200)
    assert status == 413 and read == 0


async def test_an_undeclared_oversized_body_is_cut_off():
    body = b"x" * 1000
    _, _, read = await _call(_mw(_guard(), max_body=100), _scope(),
                             body=body, declare=False, chunk=50)
    assert read < len(body)      # the app never reads the whole thing
    assert read <= 100 + 50      # bounded to the cap plus at most one streamed chunk


# -- headers ----------------------------------------------------------------------------

async def test_security_headers_ride_the_response():
    _, headers, _ = await _call(_mw(_guard()), _scope())
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors" in headers.get("content-security-policy", "")


async def test_hsts_only_when_the_edge_is_https():
    _, plain, _ = await _call(_mw(_guard(), https=False), _scope())
    assert "strict-transport-security" not in plain
    _, behind_proxy, _ = await _call(_mw(_guard(), https=False), _scope(
        headers={"x-forwarded-proto": "https"}))
    assert "strict-transport-security" in behind_proxy


async def test_a_normal_small_request_passes_through():
    status, headers, read = await _call(_mw(_guard()), _scope(), body=b'{"ok":1}')
    assert status == 200 and read == 8
