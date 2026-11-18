"""The edge, for when the manager faces more than localhost.

Nothing in here is a second authentication — the gate owns that. This is the layer that
keeps one rude or hostile caller from spending the whole machine: a request-rate ceiling, a
lockout after repeated bad credentials, a cap on how large a body we will read, and the
response headers a browser wants on an HTTP server. It is deliberately in-memory and
per-process: a manager behind a real proxy (Tailscale, Caddy) is one process, and a counter
that survives a restart is a counter that also survives the bug that filled it.

Who a request *is* comes from the socket, unless `trust_forwarded_for` says a trusted proxy
sits in front — then from the last hop of X-Forwarded-For, which that proxy sets and a client
cannot forge past it. A request the proxy did not stamp (no X-Forwarded-For, with the port
bound to localhost) is the operator reaching it directly, and is never rate-limited or locked
out: the portal and the admin API are localhost-only by design, and locking the owner out of
their own machine is not a security property worth having. In Docker turn `trust_forwarded_for`
on, because the socket peer there is always the bridge gateway — never the real caller, and
never loopback — so X-Forwarded-For is the only thing that tells callers apart.
"""

import logging
import time
from collections import defaultdict, deque

log = logging.getLogger("mcpm")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _header(scope, name: bytes) -> bytes:
    for k, v in scope.get("headers", []):
        if k == name:
            return v
    return b""


def client_addr(scope, trust_forwarded_for: bool) -> str:
    """The address we hold a caller to. Behind a trusted proxy that is the real client; the
    rightmost X-Forwarded-For hop is the one that proxy wrote, so it cannot be spoofed from
    outside. Otherwise it is the peer on the socket."""
    if trust_forwarded_for:
        xff = _header(scope, b"x-forwarded-for").decode(errors="replace")
        if xff:
            return xff.split(",")[-1].strip()
    peer = scope.get("client")
    return peer[0] if peer else "?"


def _is_local(scope, trust_forwarded_for: bool) -> bool:
    """The operator, who is never rate-limited or locked out. A request carrying
    X-Forwarded-For came through a proxy and is never the operator. With a trusted proxy in
    front, a request WITHOUT that header reached the (localhost-bound) port directly -- so it
    is this machine, whatever the socket says; this is the case that matters in Docker, where
    the peer is always the bridge gateway and never loopback. Without a trusted proxy, the
    older test holds: a loopback peer with no proxy header, which is right for a direct bind."""
    if _header(scope, b"x-forwarded-for"):
        return False
    if trust_forwarded_for:
        return True
    peer = scope.get("client")
    return bool(peer) and peer[0] in _LOOPBACK \
        and not _header(scope, b"tailscale-funnel-request")


class Guard:
    """Per-caller request-rate and bad-credential state, in memory. One instance is shared by
    the Limits middleware (which enforces the ban and the rate) and the Gate (which reports a
    failed credential through .fail and a good one through .ok)."""

    def __init__(self, cfg: dict):
        self.rate = int(cfg.get("rate_per_minute", 240))          # 0 disables the rate limit
        self.window = 60.0
        self.max_fails = int(cfg.get("max_auth_failures", 8))
        self.fail_window = float(cfg.get("auth_window_seconds", 300))
        self.ban0 = float(cfg.get("ban_seconds", 300))
        self.max_ban = float(cfg.get("max_ban_seconds", 86400))
        self._hits: dict[str, deque] = defaultdict(deque)   # addr -> recent request times
        self._fails: dict[str, deque] = defaultdict(deque)  # addr -> recent failure times
        self._until: dict[str, float] = {}                  # addr -> banned until (epoch)
        self._last_ban: dict[str, float] = {}               # addr -> last ban length, to double

    def banned(self, addr: str) -> float:
        """Seconds left on a ban, or 0.0 if the caller is free."""
        until = self._until.get(addr)
        if until is None:
            return 0.0
        left = until - time.monotonic()
        if left <= 0:
            self._until.pop(addr, None)
            return 0.0
        return left

    def allow(self, addr: str) -> bool:
        """True if the caller is under the rate ceiling; records this request when it is."""
        if self.rate <= 0:
            return True
        now = time.monotonic()
        hits = self._hits[addr]
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.rate:
            return False
        hits.append(now)
        if not hits:
            self._hits.pop(addr, None)
        return True

    def fail(self, addr: str) -> None:
        """A bad admin token or key from this caller. Enough of them inside the window earns an
        escalating ban — the first short, each repeat twice as long, capped."""
        now = time.monotonic()
        fails = self._fails[addr]
        cutoff = now - self.fail_window
        while fails and fails[0] < cutoff:
            fails.popleft()
        fails.append(now)
        if len(fails) >= self.max_fails:
            length = min(self._last_ban.get(addr, 0.0) * 2 or self.ban0, self.max_ban)
            self._last_ban[addr] = length
            self._until[addr] = now + length
            self._fails.pop(addr, None)
            log.warning("locked out %s for %s after %d bad credentials",
                        addr, _fmt(length), self.max_fails)

    def ok(self, addr: str) -> None:
        """A good credential clears this caller's failures and lifts any ban: a client that can
        prove who it is is not the thing the lockout is for."""
        self._fails.pop(addr, None)
        if self._until.pop(addr, None) is not None:
            log.info("lockout on %s lifted by a valid credential", addr)


def _fmt(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


class Limits:
    """Outermost middleware: ban, rate, body cap, and the browser's security headers. It runs
    before CORS and the gate so an abusive caller is turned away before any work is done, and
    so a 429 still carries the headers a response should."""

    def __init__(self, app, guard: Guard, *, trust_forwarded_for: bool,
                 max_body_bytes: int, security_headers: bool, https: bool):
        self.app = app
        self.guard = guard
        self.trust = trust_forwarded_for
        self.max_body = max_body_bytes
        self.headers = security_headers
        self.https = https

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        exempt = _is_local(scope, self.trust)
        addr = client_addr(scope, self.trust)

        if not exempt:
            left = self.guard.banned(addr)
            if left:
                return await self._deny(scope, send, 429, "locked out: too many failed attempts",
                                        retry_after=int(left) + 1)
            if not self.guard.allow(addr):
                return await self._deny(scope, send, 429, "rate limit exceeded",
                                        retry_after=int(self.guard.window))

        # A body larger than the cap is refused up front when the caller declares it, and cut
        # off mid-stream when it does not (a dropped read ends the request without our reading
        # the whole thing into memory).
        if self.max_body > 0:
            cl = _header(scope, b"content-length")
            if cl.isdigit() and int(cl) > self.max_body:
                return await self._deny(scope, send, 413, "request body too large")
            receive = self._capped(receive)

        send = self._headed(scope, send) if self.headers else send
        return await self.app(scope, receive, send)

    def _capped(self, receive):
        seen = 0
        cut = False

        async def wrapped():
            nonlocal seen, cut
            if cut:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_body:
                    cut = True
                    log.warning("request body over %d bytes cut off", self.max_body)
            return msg

        return wrapped

    def _headed(self, scope, send):
        https = self.https or scope.get("scheme") == "https" \
            or _header(scope, b"x-forwarded-proto") == b"https"

        async def wrapped(msg):
            if msg["type"] == "http.response.start":
                h = msg.setdefault("headers", [])
                h.append((b"x-content-type-options", b"nosniff"))
                h.append((b"x-frame-options", b"DENY"))
                h.append((b"referrer-policy", b"no-referrer"))
                h.append((b"content-security-policy", b"frame-ancestors 'none'"))
                if https:
                    h.append((b"strict-transport-security",
                              b"max-age=63072000; includeSubDomains"))
            await send(msg)

        return wrapped

    async def _deny(self, scope, send, status, msg, retry_after=None):
        headers = [(b"content-type", b"application/json")]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        if self.headers:
            headers += [(b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body",
                    "body": ('{"error":"%s"}' % msg).encode()})
