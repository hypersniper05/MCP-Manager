"""The edge layer on its own: rate ceiling, lockout, body cap, headers, and who a caller is.

No network and no manager process -- the middleware is an ASGI callable, so a hand-built
scope/receive/send drives it directly and deterministically."""

import pytest

from mcp_manager.limits import Guard, Limits, client_addr, host_allowed, sdk_hosts, _is_local


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


def _mw(guard, *, trust=False, max_body=1 << 20, headers=True, https=False, hosts=None):
    return Limits(None, guard, trust_forwarded_for=trust,
                  max_body_bytes=max_body, security_headers=headers, https=https,
                  allowed_hosts=hosts)


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

def test_a_lockout_escalates_for_a_repeat_offender():
    g = _guard(max_auth_failures=3, ban_seconds=10, max_ban_seconds=100)
    for _ in range(3):
        g.fail("4.4.4.4")
    first = g.banned("4.4.4.4")
    assert 0 < first <= 10
    # a repeat offender's next ban is longer
    for _ in range(3):
        g.fail("4.4.4.4")
    assert g.banned("4.4.4.4") > first


def test_a_good_credential_clears_the_count_but_not_a_ban_in_force():
    g = _guard(max_auth_failures=3, ban_seconds=10)
    g.fail("4.4.4.4"); g.fail("4.4.4.4")
    g.ok("4.4.4.4")                      # a typo or two, then the right key
    g.fail("4.4.4.4"); g.fail("4.4.4.4")
    assert g.banned("4.4.4.4") == 0.0    # the count started over
    g.fail("4.4.4.4")
    assert g.banned("4.4.4.4") > 0
    g.ok("4.4.4.4")
    assert g.banned("4.4.4.4") > 0       # a ban runs out; a good key does not jump it


def test_stale_callers_are_forgotten(monkeypatch):
    # every address that ever called must not be kept for good
    import mcp_manager.limits as limits
    clock = {"t": 1000.0}
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock["t"])
    g = _guard(rate_per_minute=5, max_auth_failures=2, ban_seconds=10, max_ban_seconds=100)
    for i in range(500):
        g.allow(f"10.0.{i // 250}.{i % 250}")
    g.fail("6.6.6.6"); g.fail("6.6.6.6")          # banned 10 s, with a repeat-offender record
    g.fail("7.7.7.7")                             # one stray failure
    assert g.tracked() == 502
    clock["t"] += 61                              # past the rate window and the ban
    g.allow("1.1.1.1")
    # left: 1.1.1.1 (active), 6.6.6.6's record (kept a max ban past its end), 7.7.7.7's
    # failure (inside its 300 s window)
    assert g.tracked() == 3 and g.banned("6.6.6.6") == 0.0
    clock["t"] += 400                             # past the fail window and ban end + max_ban
    g.allow("1.1.1.1")
    assert g.tracked() == 1                       # only the caller still active


def test_a_flood_of_warnings_is_logged_once_a_minute(monkeypatch, caplog):
    import logging
    import mcp_manager.limits as limits
    clock = {"t": 5000.0}
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock["t"])
    limits._quiet.clear()
    with caplog.at_level(logging.WARNING, logger="mcpm"):
        for _ in range(100):
            limits._warn("test", "noise %s", "x")
        clock["t"] += 61
        limits._warn("test", "noise %s", "x")
    lines = [r.getMessage() for r in caplog.records if "noise" in r.getMessage()]
    assert len(lines) == 2 and "+99 more" in lines[1]


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
    # this host only: never a policy for names under it that the manager does not own
    assert "includesubdomains" not in behind_proxy["strict-transport-security"].lower()


async def test_a_normal_small_request_passes_through():
    status, headers, read = await _call(_mw(_guard()), _scope(), body=b'{"ok":1}')
    assert status == 200 and read == 8


# -- Host check (DNS rebinding) ---------------------------------------------------------

def test_host_entries_match_exact_bare_and_any_port():
    allowed = ["box.ts.net:*", "10.0.0.5:8899", "plain.example"]
    assert host_allowed("box.ts.net:8443", allowed)
    assert host_allowed("BOX.ts.net", allowed)          # no port (443), any case
    assert host_allowed("10.0.0.5:8899", allowed)
    assert not host_allowed("10.0.0.5:9999", allowed)   # an exact entry pins the port
    assert host_allowed("plain.example", allowed)
    assert not host_allowed("plain.example:80", allowed)
    assert not host_allowed("box.ts.net.evil.example", allowed)
    assert not host_allowed("evilbox.ts.net:8443", allowed)
    assert not host_allowed("", allowed)


def test_loopback_names_are_always_this_machine():
    for h in ("127.0.0.1:8899", "localhost:8899", "localhost", "[::1]:8899"):
        assert host_allowed(h, ["box.ts.net:*"])
    assert not host_allowed("localhost.evil.example", ["box.ts.net:*"])


def test_the_sdk_gets_the_same_list_in_its_own_syntax():
    out = sdk_hosts(["Box.ts.net:*"])
    assert "box.ts.net:*" in out and "box.ts.net" in out
    assert "127.0.0.1:*" in out and "localhost" in out


@pytest.mark.parametrize("path", ["/", "/api/v1/keys", "/mcp"])
async def test_an_unknown_host_is_refused_on_every_path(path):
    mw = _mw(_guard(), hosts=["box.ts.net:*"])
    status, headers, read = await _call(mw, _scope(path=path, headers={"host": "evil.example:8899"}),
                                        body=b'{"x":1}')
    assert status == 421 and read == 0
    assert headers.get("x-content-type-options") == "nosniff"


async def test_a_listed_host_and_localhost_pass():
    mw = _mw(_guard(), hosts=["box.ts.net:*"])
    status, _, _ = await _call(mw, _scope(headers={"host": "box.ts.net:8443"}))
    assert status == 200
    status, _, _ = await _call(mw, _scope(path="/", headers={"host": "127.0.0.1:8899"}))
    assert status == 200


async def test_without_a_list_the_host_is_not_checked():
    status, _, _ = await _call(_mw(_guard()), _scope(headers={"host": "anything.example"}))
    assert status == 200


async def test_a_flood_of_made_up_hosts_is_held_to_the_rate_ceiling():
    mw = _mw(_guard(rate_per_minute=3), hosts=["box.ts.net:*"])
    for _ in range(3):
        status, _, _ = await _call(mw, _scope(headers={"host": "evil.example"}))
        assert status == 421
    status, _, _ = await _call(mw, _scope(headers={"host": "evil.example"}))
    assert status == 429


# -- the gate reports failures, but never the operator's ---------------------------------

async def test_the_gate_counts_a_remote_bad_token_but_not_the_operators():
    from mcp_manager.gate import Gate
    g = _guard(max_auth_failures=1, ban_seconds=60)

    async def app(scope, receive, send):
        raise AssertionError("a bad token must not reach the app")

    gate = Gate(app, db=None, admin_token="right", guard=g, trust_forwarded_for=True)

    async def run(headers):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        hs = [(b"authorization", b"Bearer wrong")] + [(k.encode(), v.encode()) for k, v in headers.items()]
        await gate({"type": "http", "path": "/api/v1/keys", "method": "GET",
                    "client": ["172.17.0.1", 1], "headers": hs}, receive, send)
        return sent[0]["status"]

    assert await run({}) == 401                       # the operator (no proxy header)...
    assert g.banned("172.17.0.1") == 0.0              # ...is not counted
    assert await run({"x-forwarded-for": "8.8.4.4"}) == 401
    assert g.banned("8.8.4.4") > 0                    # a remote caller is


async def test_a_bad_byte_in_the_token_is_a_401_and_counts():
    # a header that is not UTF-8 used to raise inside the gate: a 500, a traceback, and no
    # failure counted toward the lockout
    from mcp_manager.gate import Gate
    g = _guard(max_auth_failures=1, ban_seconds=60)

    async def app(scope, receive, send):
        raise AssertionError("a bad token must not reach the app")

    gate = Gate(app, db=None, admin_token="right", guard=g, trust_forwarded_for=True)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    for token in (bytes([0xFF, 0xFE]), "\u00e9t\u00e9".encode()):
        sent.clear()
        await gate({"type": "http", "path": "/api/v1/keys", "method": "GET", "client": ["172.17.0.1", 1],
                    "headers": [(b"authorization", b"Bearer " + token),
                                (b"x-forwarded-for", b"8.8.8.8")]}, receive, send)
        assert sent[0]["status"] == 401
    assert g.banned("8.8.8.8") > 0
