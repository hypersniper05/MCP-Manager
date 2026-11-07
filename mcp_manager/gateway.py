"""The gateway: one flat, per-group view over a pool of upstreams.

Everything the manager owns lives here — groups, overrides, usage events, the two native
tools. Transport and JSON-RPC framing are the SDK's job and we stay out of them.

A group has one of three modes:

  direct    every tool from every server is in the client's list. Nothing to search.
  hybrid    the group's defaults are in the list, plus search_tools and call_tool. Most of the
            frontends we serve (LM Studio, Open WebUI, SillyTavern, the llama.cpp webui) build
            their function list once and never ask again, so nothing depends on a re-list:
            call_tool reaches every tool on every client alike.
  adaptive  hybrid, plus up to ten tools per key that its app is likely to reach for, learned
            from its own calls (adaptive.py). They join the list the client fetches; the manager
            never interrupts a conversation just to reshuffle them -- a changed tool list costs
            the client its prompt cache.

Every round trip is spent deliberately -- a tool result is a message we can put anything in:

  known name      call_tool                      1 -- any tool is callable by name
  unknown         search_tools -> call_tool      2 -- matches carry their arguments
  name misspelt   call_tool                      1 -- listTabs runs list_tabs, and says so
  wrong name      call_tool -> call_tool         2 -- the refusal is "did you mean X", with usage
  arguments off   call_tool                      1 -- "1000" for an integer is fixed, and said so
  arguments wrong call_tool -> call_tool         2 -- refused here, with exactly what to send
  first use of a server in a conversation        0 -- the result lists all its other tools, and
                                                      clients that re-list see them in the list

Naming rule, settled by real data: tool names pass through raw, and only collisions get
namespaced. Renaming has to apply to BOTH what we list and what we send upstream — do it
on one side and the tool silently vanishes and every call to it fails.
"""

import asyncio
import difflib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime

import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server

from . import adaptive, db, gate, hints, scoring
from .hints import short

log = logging.getLogger("mcpm")

# Lands in the tool section of the chat template, next to the tool definitions, so it is
# reference text ABOUT the toolset -- no persona, none of our vocabulary.
INSTRUCTIONS = {
    "direct": "{servers}\n\nEvery tool on these servers is in your tool list.",
    "hybrid": (
        "{servers}\n\n"
        "The tools in your list are ready to call. Everything else on these servers runs "
        "through call_tool: if you know a tool's name, call it directly; if not, call "
        "search_tools with a few words about the step first -- each match shows its arguments "
        "(list_mcpservers and get_tools_from_server show what each server has). The first time you use a server, the result lists all of that server's tools, so you "
        "will not need to search it again. Mistakes are answered with the fix: a wrong name "
        "gets the closest match, wrong arguments get the right ones."
    ),
}

# to the model, learned tools are just tools in its list: nothing new to explain
INSTRUCTIONS["adaptive"] = INSTRUCTIONS["hybrid"]
SEARCHING = ("hybrid", "adaptive")     # the modes that list the manager's own tools

SEARCH_PAGE = 5      # matches per search page: each carries its arguments, so five is plenty
OPEN_IDLE = 3600     # a server stays open to a conversation for an hour after its last use
# two calls of one app this close are one stretch of work: the next is a chain, not a new start
# (the session gap the adaptive ranking was measured with)
CHAIN_GAP = 1800
# Conversation bookkeeping asks "did the client list before or after it opened that server?"
# -- often a few milliseconds apart. time.monotonic() ticks every ~15 ms on Windows and calls
# those a tie; perf_counter does not.
_clock = time.perf_counter

# what models actually put in a call_tool call, besides the documented shape
NAME_KEYS = ("name", "tool", "tool_name", "toolName", "function")
ARG_KEYS = ("arguments", "args", "params", "parameters", "input", "kwargs")
QUERY_KEYS = ("query", "q", "text", "description", "task", "search")
# an upstream error that is about the call's shape, so the usage is the answer to it
ARG_ERROR = re.compile(r"argument|param|valid|required|missing|expected|schema|field|unknown|"
                       r"unrecognized|type", re.I)
# an upstream error that says the tool could not do its job here (blocked, empty, unreachable) --
# the moment to point at a tool on another server that might
FETCH_ERROR = re.compile(r"\b40[134]\b|block|denied|forbidden|captcha|bot detection|javascript|"
                         r"empty|unreachable|refused|timed? ?out|could not (fetch|load|reach|open)", re.I)


# what a model puts in a call to name a server, besides `server`
SERVER_KEYS = ("server", "mcp", "mcp_server", "server_name", "upstream", "name")


def native_tools(servers: str) -> list[types.Tool]:
    """The manager's own tools, for a hybrid group. Four, and short: their descriptions ride along
    with every request a client makes.

    The server index goes in search_tools' own description, not only in the instructions:
    plenty of clients never show a server's instructions to the model, but every client shows
    it the tool descriptions. It is as old as the client's last tools/list, though --
    list_mcpservers is the live one.
    """
    return [
        types.Tool(
            name="search_tools",
            description=(
                "Find tools by what they do: describe one step in a few words, e.g. \"download a "
                "youtube video\"; `server` searches one server only. Each match shows its arguments; "
                "run it with call_tool.\nServers:\n" + servers),
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "description": "a few words about the step"},
                "server": {"type": "string", "description": "only this server's tools (default: all)"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "which page of matches (default 1)"},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 20,
                              "description": f"matches per page (default {SEARCH_PAGE})"}},
                "required": ["query"]},
        ),
        types.Tool(
            name="call_tool",
            description=(
                "Run any tool on the connected servers by name: one search_tools showed you, one "
                "a previous result listed, or one you already know. A wrong name or wrong "
                "arguments are answered with the fix."),
            inputSchema={"type": "object", "properties": {
                "name": {"type": "string", "description": "the tool's name"},
                "arguments": {"type": "object", "description": "that tool's own arguments"}},
                "required": ["name"]},
        ),
        types.Tool(
            name="list_mcpservers",
            description="The servers behind this manager, live: name, what each is for, tool count, up or down.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_tools_from_server",
            description=("Every tool on one server: what each does and its argument names (? = optional). "
                         "Run any with call_tool; search_tools shows the arguments in full."),
            inputSchema={"type": "object", "properties": {
                "server": {"type": "string", "description": "a server name, as listed above"}},
                "required": ["server"]},
        ),
    ]


# The manager's own tools by spelling, plus the names models reach for. call_tool is not here:
# it is the door, and is handled before any name is looked up.
NATIVE_BY_NORM = {hints.norm(alias): tool for tool, aliases in {
    "search_tools": ("search_tools", "search_tool"),
    "list_mcpservers": ("list_mcpservers", "list_servers", "get_servers", "list_mcp"),
    "get_tools_from_server": ("get_tools_from_server", "get_server_tools", "list_server_tools",
                              "list_tools_on_server", "server_tools"),
}.items() for alias in aliases}


def _reason(e: BaseException) -> str:
    """The first real cause, out of whatever exception groups anyio wrapped it in."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, asyncio.CancelledError):
        return "timed out"          # an anyio scope's deadline, surfacing as a cancel
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _auth_client(headers: dict):
    """An HTTP client carrying a server's auth headers, or None when it has none -- then the
    SDK makes its own. streamable_http_client takes no headers: they ride on the client.
    Built by the SDK's own factory so the timeouts match the default one it would have used
    (30 s operations, 300 s SSE read -- a stock client's 5 s read would cut the stream)."""
    if not headers:
        return None
    from mcp.shared._httpx_utils import create_mcp_http_client
    return create_mcp_http_client(headers=headers)


async def _close_auth_client(client) -> None:
    """Close what _auth_client made. Shielded: the reconnect path drops sessions by cancelling
    their scope, and an unshielded close would be cancelled before it ran. Bounded, because
    a client still holding a stream takes a moment to give it up."""
    if client is not None:
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            await client.aclose()


class ManagerServer(Server):
    """One Server instance serves every group; the instructions are still per-group.

    Both paths that hand a client that text -- the runner's per-request
    create_initialization_options(), and the server/discover handler -- read
    self.instructions at call time, inside the request's own task. So overriding it with a
    property that consults the identity the gate already resolved makes them per-group for
    free: no handler to replace, no shared mutable attribute, no race between concurrent
    sessions.
    """

    def __init__(self, gateway, **kwargs):
        self.gateway = gateway          # must exist before super() assigns self.instructions
        super().__init__(**kwargs)

    @property
    def instructions(self):
        return self.gateway.instructions()

    @instructions.setter
    def instructions(self, value):
        pass   # the text comes from the gateway and config, not from the constructor

    def create_initialization_options(self, notification_options=None, *args, **kwargs):
        # Advertise listChanged, because using a server opens it into a hybrid session's list.
        # Clients that listen re-list and get real entries; the rest lose nothing, because the
        # same call result also spells out every tool the server opened.
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True), *args, **kwargs)


HANDSHAKE_MS = 30000   # connect, list, health check: a server still starting up gets this long
CALL_TIMEOUT = 600     # a tool call: jobs like a download with separation legitimately take minutes
RETRY_CAP = 60         # longest wait between reconnect attempts: a server that comes back is found
                       # within a minute, and one attempt a minute at a dead host costs nothing
# One SSE event can legitimately be big: a server with a few hundred tools answers tools/list
# with a single event holding the whole catalogue, over the SDK's 1 MB default. That default
# guards against untrusted servers; ours are chosen by the admin, so we raise it -- bounded,
# because a runaway stream should still not eat the manager's memory.
SSE_EVENT_MAX = 32 * 1024 * 1024


class Link:
    """One upstream's connection. Its own task opens it, holds it and closes it, because
    anyio cancel scopes are task-bound: a connection opened in one task cannot be closed
    from another. One task per server also means a dead server costs only itself -- one
    shared owner used to make every server wait behind the slowest one.

    `tools` is what the server last answered with, and it is NOT cleared when the server goes
    down: a down server's tools keep their names, stay in lists and in search, and a call to
    one is answered "down right now, the manager keeps retrying" -- not "no such tool".
    """

    def __init__(self, row):
        self.name, self.url = row["name"], row["url"]
        self.headers = json.loads(row["headers"] or "{}")   # auth for servers that need it; sent as-is
        self.timeout = (row["timeout_ms"] or HANDSHAKE_MS) / 1000
        self.shared = bool(row["shared"])   # one session per GROUP here, not per key (ClientLink)
        self.session = None
        self.tools = _load_tools(row["tools"])
        self.instructions = None
        self.title = None
        self.error = None
        self.connecting = False
        self.scope = None               # cancelling it drops the session at once (see _lost)
        self.down_since = None          # monotonic; None while connected
        self.last_try = float("-inf")
        self.next_try = None            # monotonic time of the next attempt, while down
        self.wake = asyncio.Event()     # connected: check the connection now. down: retry now
        self.settled = asyncio.Event()  # the latest attempt has finished, either way
        self.task = None


CLIENT_IDLE = 900      # a client's own session to a server is closed after 15 minutes unused


def _prefix(upstream: str) -> str:
    """A server's name as it goes in front of a tool name when two servers share one: spaces,
    and anything else a client would reject in a tool name, become "_". OpenAI-style clients
    (llama.cpp among them) take only [A-Za-z0-9_-] in a function name -- "Image Generator__x"
    would sink a whole tool list there. Runs collapse to one "_", so "__" stays the separator."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_.-]", "_", upstream.strip())).strip("_") or "server"


def _and(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _some(names, n=3) -> str:
    """The first few names and how many more: what a person can take in at a glance."""
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def _who(owner) -> str:
    return f"group {owner[1]}" if isinstance(owner, tuple) else f"key {owner}"


class ClientLink:
    """One client's own session to one server -- every tool call runs on one of these.

    A server keeps per-client state per MCP session: a browser's tabs and logins, a job list,
    a handle. One shared session would make every app behind the manager one client to it, able
    to read and wreck each other's state. So each key gets its own session to each server it
    uses, opened on its first call there and closed politely when it has been idle a while, or
    its key is revoked, or the server is removed. Generic on purpose: nothing about any server
    is known here. The server's shared Link only lists tools and watches health; it never runs
    a tool, so it holds no per-client state of its own.

    The one exception is the admin's, per server: "shared per group". Then the owner is the
    group, ("group", id), and every key in it reaches the same session -- one memory, one browser
    -- for one person's devices. Off by default; isolation is the rule.
    """

    def __init__(self, owner, link):
        self.owner, self.upstream, self.url, self.timeout = owner, link.name, link.url, link.timeout
        self.headers = dict(link.headers)   # the server's auth applies to a client's session too
        self.session = None
        self.scope = None
        self.task = None
        self.error = None
        self.busy = 0                       # calls in flight: never closed under one
        self.last_used = time.monotonic()
        self.ready = asyncio.Event()        # the first connect finished, either way
        self.stop = asyncio.Event()         # close politely now


def _stopped(e: BaseException) -> bool:
    """For a task in the group every server shares: was this a stop (a server removed, edited or
    shutting down)? Raises what must go on; True means end quietly, False means an ordinary failure.

    Only a cancellation may leave such a task. A stop can land just as a request fails, and then the
    request's error is what surfaces: raised, it would end the group, and /mcp with it, for every
    app, while the process and the admin panel go on looking healthy."""
    if isinstance(e, (KeyboardInterrupt, SystemExit)):
        raise e
    if not asyncio.current_task().cancelling():
        return False
    if isinstance(e, asyncio.CancelledError):
        raise e
    return True


def _load_tools(raw) -> list:
    try:
        return [types.Tool.model_validate(t) for t in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []


async def _reachable(url: str) -> bool:
    """Does anything accept a connection at this server's address? That is the whole question.

    Deliberately not an HTTP request: a busy server -- one answering our own slow call, say --
    can take seconds to answer anything, and some never answer a bare HEAD at all. Taking the
    connection is proof of life; refusing it, or nothing answering, is proof of death.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, port), 3)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{max(seconds, 1)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600)} h"


class Gateway:
    def __init__(self, cfg: dict, database, admin_token: str | None, portal_file: str):
        self.cfg = cfg
        self.db = database
        self.admin_token = admin_token
        self.portal_file = portal_file
        self._links = {}         # upstream name -> Link
        self._exposed = {}       # name a client calls -> (upstream, real name)
        self._names = {}         # (upstream, real name) -> name a client calls
        self._defs = {}          # (upstream, real name) -> the upstream's Tool
        self._index = None
        self._vectors = {}       # document text -> vector; text, so an edit re-embeds
        self._tg = None
        self._embed_task = None
        self._opened = {}        # conversation -> {upstream: (opened at, last used)}, monotonic
        self._listed = {}        # conversation -> when it last fetched tools/list
        self._heard = {}         # conversation -> ({server: state} it was last told, when)
        self._shown = {}         # conversation -> {server: when get_tools_from_server listed it}
        self._clients = {}       # (key id, upstream) -> ClientLink: each client's own session
        self._picked = {}        # conversation -> the learned tools in the list it last fetched
        self._moved = {}         # server renamed this run: old name -> new (defaults follow it)
        self.adaptive = adaptive.Adaptive()   # every key's habits, relearned from the log at start
        # The embedding model that runs in this process (embedder.py). Not loaded yet, or not
        # loadable, is a supported state: search is lexical until it is.
        self.embed_cfg = cfg.get("embeddings") or {}

    def build(self) -> Server:
        return ManagerServer(
            self,
            name="mcp-manager",
            version="0.0.1",
            description="Curated access to several MCP servers.",
            lifespan=self._lifespan,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    # ---- lifecycle -----------------------------------------------------------

    @asynccontextmanager
    async def _lifespan(self, _server):
        async with asyncio.TaskGroup() as tg:
            self._tg = tg
            startup = asyncio.create_task(self._startup())
            try:
                yield {}
            finally:
                # Close every client's session politely first -- that is what frees whatever a
                # server set aside for it (a browser worker, say) -- but not for long.
                clients = [c.task for c in self._clients.values() if c.task]
                for c in list(self._clients.values()):
                    c.stop.set()
                if clients:
                    await asyncio.wait(clients, timeout=5)
                # Then stop everything else we started, or the task group waits on it forever
                # and a SIGTERM turns into a SIGKILL ten seconds later.
                self._tg = None
                for task in clients:
                    task.cancel()
                for task in [startup, self._embed_task, *(l.task for l in self._links.values())]:
                    if task:
                        task.cancel()

    async def _startup(self):
        # Servers first. The embedding model can be a 330 MB download on first run, and
        # nothing about connecting a server should wait behind that.
        try:
            await self.reconcile(wait=0)
            self.learn()     # after the catalogue is in, so a group's defaults resolve
            if self.embed_cfg.get("file"):
                from . import embedder
                await asyncio.to_thread(embedder.ensure_model, self.embed_cfg)
                self._kick_embed()
        except Exception:   # noqa: BLE001
            log.exception("startup failed")

    async def reconcile(self, wait: float = 20):
        """Make the running links match the database: start new ones, stop removed, disabled
        or re-pointed ones. Then wait (bounded) for the new ones' first attempt, so the API
        call that caused this can answer with the truth instead of 'connecting'."""
        if self._tg is None:
            return          # called before the app started (tests, tooling)
        rows = {r["name"]: r for r in self.db.execute("SELECT * FROM upstreams WHERE enabled = 1")}
        for name, link in list(self._links.items()):
            row = rows.get(name)
            # a finished task is a link that died without being stopped: restart it.
            # New headers mean the open session was made without them -- same restart.
            if (row is None or row["url"] != link.url or link.task.done()
                    or json.loads(row["headers"] or "{}") != link.headers
                    or (row["timeout_ms"] or HANDSHAKE_MS) / 1000 != link.timeout):
                await self._stop(name)
            elif bool(row["shared"]) != link.shared:
                # per key <-> per group: the open sessions belong to the old owners. Close them;
                # each next call opens its session under the new rule.
                link.shared = bool(row["shared"])
                self.close_clients(upstream=name)
        started = []
        for name, row in rows.items():
            if name not in self._links:
                link = self._links[name] = Link(row)
                link.task = self._tg.create_task(self._run(link))
                link.task.add_done_callback(self._ended)
                started.append(link)
        self._changed()
        if started and wait:
            with anyio.move_on_after(wait):
                for link in started:
                    await link.settled.wait()

    async def reconnect(self, name, wait: float = 20):
        """A fresh start with one server, asked for from the settings: its connection is dropped
        and opened again -- a new session, its headers sent again (so a changed or expired
        credential is used afresh), its tools listed again -- and the apps' own sessions on it
        close, to open again on their next call. Waits (bounded) for the first attempt, so the
        answer already says how it went."""
        log.info("upstream %s: reconnect asked for in the settings", name)
        await self._stop(name)
        await self.reconcile(wait=wait)

    async def _stop(self, name):
        link = self._links.pop(name, None)
        self.close_clients(upstream=name)      # removed, disabled or re-pointed: theirs go too
        if link and link.task:
            link.task.cancel()
            await asyncio.wait({link.task}, timeout=10)   # closes in its own task; don't hang on it
        self._changed()

    # ---- each client's own sessions --------------------------------------------

    def _owner(self, link):
        """Whose session a call runs on: the calling key's -- or, on a server shared per group,
        its group's, so every key in the group meets the same state there."""
        if link.shared:
            gid = self.group()["id"]
            if gid is not None:
                return ("group", gid)
        return gate.current_key.get()

    async def _client(self, owner, link: Link) -> ClientLink:
        """This owner's session to this server (a key's, or a group's on a shared server), opened
        on its first call there. Waits (up to the server's connect limit) for a new one to
        finish connecting, either way."""
        ck = (owner, link.name)
        cl = self._clients.get(ck)
        if cl is not None and (cl.task is None or cl.task.done() or cl.stop.is_set() or cl.url != link.url):
            self._clients.pop(ck, None)
            cl = None
        if cl is None:
            cl = self._clients[ck] = ClientLink(owner, link)
            if self._tg is not None:
                cl.task = self._tg.create_task(self._run_client(cl))
            else:
                cl.ready.set()                  # not running (tests, tooling): nothing to open
        cl.last_used = time.monotonic()
        if cl.session is None:
            with anyio.move_on_after(cl.timeout + 5):
                await cl.ready.wait()
        return cl

    async def _run_client(self, cl: ClientLink):
        """Open one client's session, hold it until it idles out or is told to stop, then close
        it politely -- the close is what tells the server to free what it kept for this client.
        Never raises (but for a real stop): it lives in the task group every server shares."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        http = None
        try:
            with anyio.CancelScope() as scope:
                cl.scope = scope
                http = _auth_client(cl.headers)
                async with streamable_http_client(cl.url, http_client=http,
                                                  max_sse_event_size=SSE_EVENT_MAX) as (read, write, *_):
                    async with ClientSession(read, write, read_timeout_seconds=cl.timeout) as session:
                        await session.initialize()
                        cl.session = session
                        cl.ready.set()
                        log.info("upstream %s: session opened for %s", cl.upstream, _who(cl.owner))
                        while not cl.stop.is_set() and (
                                cl.busy or time.monotonic() - cl.last_used < CLIENT_IDLE):
                            with anyio.move_on_after(30):
                                await cl.stop.wait()
                        cl.session = None                       # nothing new starts on it now
                        scope.deadline = anyio.current_time() + 10   # polite, but not forever
        except BaseException as e:  # noqa: BLE001 -- see the docstring
            if _stopped(e):
                return
            cl.error = _reason(e)
            log.info("upstream %s: session for %s ended (%s)", cl.upstream, _who(cl.owner), cl.error)
        finally:
            cl.session = None
            cl.ready.set()
            await _close_auth_client(http)   # ours only when the server has headers
            if self._clients.get((cl.owner, cl.upstream)) is cl:
                del self._clients[(cl.owner, cl.upstream)]
        log.info("upstream %s: session closed for %s", cl.upstream, _who(cl.owner))

    def close_clients(self, upstream=None, key_id=None, group_id=None, owner=None, now=False):
        """Close client sessions -- every one, a server's, a key's, a group's, or one owner's.
        A key's are its OWN sessions: a group's shared session stays for the rest of the group
        (a revoked key cannot reach it anyway -- the gate refuses it first). Politely, unless
        `now`: a server that is gone will never answer the close, so there is nothing to wait for."""
        for (k, up), cl in list(self._clients.items()):
            if ((upstream is None or up == upstream) and (key_id is None or k == key_id)
                    and (group_id is None or k == ("group", group_id)) and (owner is None or k == owner)):
                if now and cl.scope is not None:
                    cl.scope.cancel()
                cl.stop.set()
                del self._clients[(k, up)]

    def sessions(self, upstream) -> int:
        return sum(1 for (_, up), cl in self._clients.items() if up == upstream and cl.session is not None)

    def _ended(self, task):
        """A link's task should only ever end by being stopped. If one ends while still in
        the pool, say so -- the next reconcile restarts it, but silence is how a dead server
        hides."""
        if self._tg is None:
            return      # shutting down: everything ends, and that is expected
        for link in self._links.values():
            if link.task is task:
                link.session, link.connecting = None, False
                link.down_since = link.down_since or time.monotonic()
                link.error = link.error or "connection task ended unexpectedly"
                link.settled.set()
                log.warning("upstream %s: connection task ended unexpectedly", link.name)

    async def _run(self, link: Link):
        """Connect, hold, reconnect -- forever, until cancelled. Never raises otherwise: an
        exception escaping here would tear down the task group every other server lives in.

        The session lives inside a cancel scope. Dropping a dead session by raising out of it
        would run the SDK's polite close -- a DELETE to the server -- and against a server that
        takes connections but never answers, that waited out a five-minute read timeout with no
        retry scheduled. Cancelling the scope cuts the close short: the server is gone anyway.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        delay = 2
        while True:
            link.wake.clear()
            link.connecting, link.last_try = True, time.monotonic()
            http = None
            try:
                with anyio.CancelScope() as scope:
                    link.scope = scope
                    http = _auth_client(link.headers)
                    async with streamable_http_client(link.url, http_client=http,
                                                      max_sse_event_size=SSE_EVENT_MAX) as (read, write, *_):
                        async with ClientSession(read, write, read_timeout_seconds=link.timeout) as session:
                            init = await session.initialize()
                            tools = (await session.list_tools()).tools
                            link.instructions = getattr(init, "instructions", None)
                            link.title = getattr(getattr(init, "server_info", None), "title", None)
                            if link.down_since is not None:
                                log.info("upstream %s is back after %s", link.name,
                                         _ago(time.monotonic() - link.down_since))
                            link.session, link.error, link.down_since, link.next_try = session, None, None, None
                            link.connecting, delay = False, 2
                            self._set_tools(link, tools)
                            log.info("upstream %s: %d tools", link.name, len(tools))
                            link.settled.set()
                            await self._hold(link, session)
            except BaseException as e:  # noqa: BLE001 -- see the docstring
                if _stopped(e):
                    return
                link.error = _reason(e)
            finally:
                await _close_auth_client(http)   # ours only when the server has headers
            # Here either way: an error, or _lost() cancelled the scope. The tools stay: they are
            # what this server has, and will have again when it is back.
            link.session, link.connecting, link.scope = None, False, None
            link.error = link.error or "stopped answering"
            link.down_since = link.down_since or time.monotonic()
            link.next_try = time.monotonic() + delay
            link.settled.set()
            log.warning("upstream %s unavailable (%s); retrying in %ds", link.name, link.error, delay)
            with anyio.move_on_after(delay):
                await link.wake.wait()          # a call that needs this server cuts the wait short
            delay = min(delay * 2, RETRY_CAP)

    async def _hold(self, link: Link, session):
        """Keep the connection until it stops answering. Check every 30 seconds, and at once
        after a failed call: that the host still takes connections, then tools/list -- which
        also catches an upstream that restarted and forgot our session, and a server that
        gained or changed a tool.

        A slow answer is not a dead server: one health check timing out is let go. Two in a row
        are not, because a session whose server died once can stay wedged (the SDK's writer
        dies with the connection) while the host itself is back and taking connections.
        """
        from mcp.shared.exceptions import MCPError
        slow = 0
        while True:
            with anyio.move_on_after(30):
                await link.wake.wait()
            link.wake.clear()
            if not await _reachable(link.url):
                self._lost(link, "stopped answering")
                await anyio.sleep(0)                    # a checkpoint: the cancelled scope unwinds us here
            try:
                tools = (await session.list_tools()).tools
            except MCPError as e:
                if e.code == types.REQUEST_TIMEOUT and slow == 0:
                    slow += 1
                    log.info("upstream %s: health check slow; giving it another round", link.name)
                    continue
                raise
            slow = 0
            if [t.model_dump() for t in tools] != [t.model_dump() for t in link.tools]:
                log.info("upstream %s: tool list changed, %d tools", link.name, len(tools))
                self._set_tools(link, tools)

    def _set_tools(self, link: Link, tools):
        """Take a server's tool list, and keep it in the DB -- so after a restart, a server that
        is down is still known by its tools instead of vanishing from every list."""
        changed = [t.model_dump() for t in tools] != [t.model_dump() for t in link.tools]
        link.tools = tools
        if changed:
            self.db.execute("UPDATE upstreams SET tools = ? WHERE name = ? AND url = ?",
                            (json.dumps([t.model_dump(mode="json", by_alias=True, exclude_none=True)
                                         for t in tools]), link.name, link.url))
            self.db.commit()
            self._changed()

    def _lost(self, link: Link, why: str):
        """The server is gone: drop the session NOW. Say so in the link at once, so the very
        next call already reads "down", and cancel the session's scope so its owner reconnects
        without first waiting on a close the dead server will never answer."""
        link.error = why
        link.session = None
        link.down_since = link.down_since or time.monotonic()
        if link.scope is not None:
            link.scope.cancel()
        self.close_clients(upstream=link.name, now=True)   # their sessions died with it

    async def _revive(self, link: Link, wait: float = 5):
        """Something needs this server. Don't make it wait out the backoff -- retry now, and give
        the attempt a few seconds: if the server just came back, the call simply works."""
        if link.session is not None:
            return
        if not link.connecting:
            if time.monotonic() - link.last_try < 10:
                return              # it has only just tried; don't hammer a dead host
            link.settled.clear()
            link.wake.set()
        if wait:
            with anyio.move_on_after(wait):
                await link.settled.wait()

    def _down(self, link, upstream, exposed) -> str:
        """What a model is told when the tool's server is down: that it is down, that nothing
        is wrong with the call, and that the manager is on it."""
        if link is None:
            return f"{upstream} is not available right now, so {exposed} did not run."
        now = time.monotonic()
        why = f" ({link.error})" if link.error else ""
        since = f" for {_ago(now - link.down_since)}" if link.down_since else ""
        when = ("it is reconnecting right now" if link.connecting or not link.next_try
                else f"next attempt in about {_ago(max(link.next_try - now, 1))}")
        return (f"{upstream} is down right now{why}{since}, so {exposed} did not run -- nothing is "
                f"wrong with your call. The manager keeps trying to reconnect ({when}), and every "
                f"{upstream} tool works again as soon as it is back, with nothing for you to change. "
                f"Try again in a little while, or carry on with tools from other servers.")

    # ---- naming and the index ------------------------------------------------

    def _changed(self):
        """A server's tools came or went: rebuild the names, then embed anything new."""
        try:
            self.invalidate()
            self._kick_embed()
        except Exception:   # noqa: BLE001 -- called from link tasks; must not kill them
            log.exception("rebuilding the tool index failed")

    def invalidate(self):
        """Rebuild the name map, built whole and assigned whole -- so a request in flight
        never sees a catalogue half-rebuilt. Then let the groups' defaults follow any rename,
        and write down what changed, in words, for the audit."""
        old_exposed, old_collisions = self._exposed, getattr(self, "_collisions", None)
        by_name, defs = {}, {}
        for up in sorted(self._links):
            for t in self._links[up].tools:
                by_name.setdefault(t.name, []).append(up)
                defs[(up, t.name)] = t
        exposed, collisions = {}, {}
        for name, owners in by_name.items():
            if len(owners) == 1:
                exposed[name] = (owners[0], name)
            else:
                collisions[name] = owners
                for up in owners:
                    exposed[f"{_prefix(up)}__{name}"] = (up, name)   # __ not / — clients mangle paths
        self._exposed, self._defs = exposed, defs
        self._names = {key: name for name, key in exposed.items()}
        self._index = None
        # The baseline is the first build that knows any tools -- not the empty one before the
        # servers' saved lists load -- or every restart would report standing clashes as new.
        if old_collisions is not None and old_exposed and collisions != old_collisions:
            self._audit_renames(old_collisions, collisions)
        self._collisions = collisions
        self._follow_defaults(old_exposed)

    def _audit_renames(self, before, now):
        """Say, in words, which tool names changed and why: one sentence per clash between servers
        (a mirror of a whole server is one event, not sixty), and one when they no longer clash."""
        started = {}
        for name, owners in now.items():
            if before.get(name) != owners:
                started.setdefault(tuple(owners), []).append(name)
        for owners, names in started.items():
            names = sorted(names)
            log.info("collision on %s (%s) — exposing namespaced", ", ".join(names[:5]), "+".join(owners))
            who = _and(owners) + (" both" if len(owners) == 2 else " all")
            renamed = _and(self._names[(up, names[0])] for up in owners)
            db.audit(self.db, "rename",
                     f"{who} have a tool named {names[0]}, so each is renamed with its server's name: {renamed}."
                     if len(names) == 1 else
                     f"{who} have {len(names)} tools with the same names ({_some(names)}), so each is renamed "
                     f"with its server's name, e.g. {renamed}.")
        ended = {}
        for name in before.keys() - now.keys():
            owner = next((up for up, real in self._names if real == name), None)
            if owner is not None:                    # gone from every server: nothing was renamed
                ended.setdefault(owner, []).append(name)
        for owner, names in ended.items():
            names = sorted(names)
            db.audit(self.db, "rename",
                     f"{names[0]} is on one server again ({owner}), so it has its plain name again."
                     if len(names) == 1 else
                     f"{len(names)} tools are on one server again ({owner}: {_some(names)}), so they have "
                     "their plain names again.")

    def _follow_defaults(self, old_exposed):
        """A group's defaults are names, and a name can move: a second server with a tool of the
        same name renames both, and when it leaves the plain name comes back. A default follows
        the tool it named, so adding a server never breaks another group's list. A name that was
        ambiguous from the start (a restart into a collision) goes to the server the group's other
        defaults come from -- and stays flagged if nothing says which."""
        for g in self.db.execute("SELECT id, name, defaults FROM groups").fetchall():
            defaults = json.loads(g["defaults"] or "[]")
            new, notes = [], []
            for d in defaults:
                if d in self._exposed:
                    new.append(d)
                    continue
                key, why = self.resolve(d), ""
                if key is None and d in old_exposed:
                    key = old_exposed[d]
                if key is not None and key not in self._names and key[0] in self._moved:
                    key = (self._moved[key[0]], key[1])      # its server was renamed
                if key is None:
                    key = self._moved_key(d)
                if key is None:
                    key = self._guess(d, defaults)
                    why = f" (picked {key[0]}: the group's other defaults are there)" if key else ""
                now = self._names.get(key) if key else None
                new.append(now or d)
                if now and now != d:
                    notes.append((d, now, key, why))
            if notes:
                self.db.execute("UPDATE groups SET defaults = ? WHERE id = ?",
                                (json.dumps(list(dict.fromkeys(new))), g["id"]))
                self.db.commit()
                d, now, key, why = notes[0]
                db.audit(self.db, "default",
                         f'Group "{g["name"]}": its default {d} is now called {now}, so the group still '
                         f"lists {key[0]}'s {key[1]}{why}." if len(notes) == 1 else
                         f'Group "{g["name"]}": {len(notes)} defaults have new names, so the group still lists '
                         "the same tools: " + ", ".join(f"{d} is now {now}" for d, now, _, _ in notes) + ".")

    def _moved_key(self, name):
        """A default spelled with a renamed server's old name ("Old_Name__tool") -> the same tool
        under the new name, if it is there."""
        for old, new in self._moved.items():
            for p in {old, _prefix(old)}:
                if name.lower().startswith(p.lower() + "__") and (new, name[len(p) + 2:]) in self._names:
                    return new, name[len(p) + 2:]
        return None

    def rename_upstream(self, old, new):
        """A server renamed in the settings: everything that names it follows. Its usage history
        moves (the stats page, each app's adaptive ranking), the defaults that spell its tools with
        the old server name follow on the next rebuild, the groups that chose it keep it, and the
        audit says what happened.
        (Description overrides move in the API, with the rename itself.)"""
        moved = self.db.execute("UPDATE events SET upstream = ? WHERE upstream = ?", (new, old)).rowcount
        self.db.commit()
        db.server_moved(self.db, old, new)
        self._moved[old] = new
        self.learn()
        db.audit(self.db, "edit", f'Server "{old}" was renamed to "{new}". Its usage history ({moved} calls), '
                 "description edits, the group defaults that name its tools and the groups "
                 "that chose it moved with it.")

    def _guess(self, name, defaults):
        """Which server's `name` a group meant, when several have it and nothing recorded which:
        the one most of the group's other defaults come from. None if that does not decide it."""
        n = name.strip().lower()
        candidates = [k for k in self._names if k[1].lower() == n]
        if len(candidates) < 2:
            return candidates[0] if candidates else None
        homes = [k[0] for k in (self.resolve(d) for d in defaults if d != name) if k]
        ranked = sorted(candidates, key=lambda k: -homes.count(k[0]))
        best = homes.count(ranked[0][0])
        return ranked[0] if best and best > homes.count(ranked[1][0]) else None

    def renamed(self) -> list:
        """The tool names that are renamed right now, for the audit: the shared name and what
        each server's tool is called instead."""
        return [{"tool": name, "servers": [{"upstream": up, "name": self._names[(up, name)]} for up in owners]}
                for name, owners in sorted(getattr(self, "_collisions", {}).items())]

    def resolve(self, name):
        """(upstream, real name) for whatever a client or model called a tool, or None.

        Tolerant on purpose, because a refused call is a wasted round trip: the exposed name,
        any case, or a server prefix in the spellings models actually produce --
        browser__list_tabs, browser/list_tabs, browser.list_tabs, "browser / list_tabs".
        A bare name that collides across servers stays ambiguous and resolves to nothing.
        """
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        if name in self._exposed:
            return self._exposed[name]
        lowered = name.lower()
        for exposed, key in self._exposed.items():
            if exposed.lower() == lowered:
                return key
        pairs = {(up.lower(), real.lower()): (up, real) for up, real in self._names}
        pairs.update({(_prefix(up).lower(), real.lower()): (up, real) for up, real in self._names})
        for sep in ("__", "/", ".", ":"):
            i = lowered.find(sep)
            while i > 0:
                pair = (lowered[:i].strip(), lowered[i + len(sep):].strip())
                if pair in pairs:
                    return pairs[pair]
                i = lowered.find(sep, i + 1)
        return None

    def _overrides(self) -> dict:
        return {(r["upstream"], r["tool"]): r["description"]
                for r in self.db.execute("SELECT upstream, tool, description FROM tool_overrides")
                if r["description"] is not None}

    def desc(self, key, overrides=None) -> str:
        overrides = self._overrides() if overrides is None else overrides
        return overrides.get(key) or self._defs[key].description or ""

    def _titles(self) -> dict:
        """(upstream, tool) -> the title an admin gave it, where they did."""
        return {(r["upstream"], r["tool"]): r["title"]
                for r in self.db.execute("SELECT upstream, tool, title FROM tool_overrides")
                if r["title"]}

    def documents(self):
        """exposed name -> the text it is matched and embedded on: everything an edit can change
        -- the tool's title and description (yours over the server's), the server's name and its
        description. Vectors are cached by this text, so changing any of it re-embeds the tool."""
        overrides, titles = self._overrides(), self._titles()
        servers = {r["name"]: r["description"] for r in self.db.execute("SELECT name, description FROM upstreams")}
        return {name: scoring.document(key[1], self.desc(key, overrides),
                                       titles.get(key) or getattr(self._defs[key], "title", None),
                                       key[0], servers.get(key[0]))
                for name, key in self._exposed.items()}

    def index(self):
        if self._index is None:
            self._index = scoring.build(self.documents(), self._vectors)
        return self._index

    def reindex(self):
        """A description changed: the document changed, so its vector must too."""
        self._changed()

    async def reembed(self, wait: float = 60):
        """Rebuild the index and wait (bounded) for whatever changed to be embedded, so the edit
        that caused it is answered only once search already reflects it."""
        self._changed()
        task = self._embed_task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=wait)

    def _kick_embed(self):
        if not self._can_embed():
            return
        if self._embed_task is None or self._embed_task.done():
            self._embed_task = asyncio.get_running_loop().create_task(self._fill_vectors())

    def _can_embed(self) -> bool:
        if self.embed_cfg.get("file"):
            from . import embedder
            path = embedder.model_path(self.embed_cfg)
            return bool(path and path.exists())
        return False

    async def _fill_vectors(self):
        """Embed every document that has no vector yet, in the background, with a backoff.

        In the background because embedding a whole catalogue on CPU takes a while, and search
        answers lexically from the first second anyway. Retried because a first load can fail
        transiently -- giving up for good would leave search lexical forever, which is the failure
        mode nobody notices (and a later search restarts it anyway). Looped because servers may
        connect while a pass is in flight.
        """
        delay, failures = 5, 0
        try:
            while failures < 8:
                missing = sorted({t for t in self.documents().values() if t not in self._vectors})
                if not missing:
                    return
                vectors = await scoring.embed(missing, self.embed_cfg)
                if vectors and len(vectors) == len(missing):
                    for text, vec in zip(missing, vectors):
                        self._vectors[text] = scoring.normalise(vec)
                    self._index = None
                    log.info("search index: %d tools, %s", len(self._exposed), self.index()["mode"])
                    continue
                failures += 1
                log.info("embedding pass did not complete; retrying in %ds", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
            log.warning("embeddings never completed: search stays lexical")
        except Exception:   # noqa: BLE001
            log.exception("embedding pass failed: search stays lexical")

    # ---- groups ------------------------------------------------------------

    def group(self, name=None):
        """The caller's group -- from the identity the gate resolved, unless named. With no
        key required and no default group in the DB, a caller gets a hybrid view with no
        defaults: the smallest view, never the largest."""
        row = self.db.execute("SELECT * FROM groups WHERE name = ?",
                              (name or gate.current_group.get() or self.cfg["default_group"],)).fetchone()
        return row or {"id": None, "name": "-", "mode": "hybrid", "defaults": "[]", "servers": None}

    @staticmethod
    def _chosen(group):
        """The servers this group is limited to, or None: all of them, whenever they were added."""
        return None if group["servers"] is None else set(json.loads(group["servers"]))

    def _visible(self, group) -> dict:
        """{name: (server, tool)} for what this group may reach. Whatever a client can list, find
        or call comes through here: a server left out of a group does not exist for its keys --
        not in a list, a search, a suggestion or the news."""
        chosen = self._chosen(group)
        return self._exposed if chosen is None else {n: k for n, k in self._exposed.items() if k[0] in chosen}

    def _server_rows(self, group) -> list:
        """The enabled servers this group reaches, by name."""
        chosen = self._chosen(group)
        return [r for r in self.db.execute("SELECT name, description FROM upstreams WHERE enabled = 1 ORDER BY name")
                if chosen is None or r["name"] in chosen]

    def close_unreachable(self):
        """A group's servers changed or a key moved: a session on a server its owner can no longer
        reach closes now -- what the server keeps for it must not outlive the access. An owner
        that can still reach the server keeps its session."""
        rows = self.db.execute("SELECT k.id, k.group_id, g.servers FROM keys k "
                               "JOIN groups g ON g.id = k.group_id").fetchall()
        reach = {r["id"]: self._chosen(r) for r in rows}
        reach.update({("group", r["group_id"]): self._chosen(r) for r in rows})
        for owner, up in list(self._clients):
            chosen = reach.get(owner)
            if chosen is not None and up not in chosen:
                self.close_clients(upstream=up, owner=owner)

    def unresolved(self, group) -> list:
        """The group's defaults that lead nowhere right now: no tool by that name (a server down, a
        tool renamed upstream), or one on a server the group does not reach."""
        chosen = self._chosen(group)

        def lost(default):
            key = self.resolve(default)
            return key is None or (chosen is not None and key[0] not in chosen)
        return [d for d in json.loads(group["defaults"] or "[]") if lost(d)]

    def members(self, group, conv=None, picks=True) -> list[str]:
        """The upstream tools in this group's list, by the name it calls them: in hybrid,
        the defaults, then every tool of each server this conversation has opened; adaptive
        adds the calling key's learned tools after the defaults."""
        visible = self._visible(group)
        if group["mode"] == "direct":
            return list(visible)
        keys = [self.resolve(d) for d in json.loads(group["defaults"] or "[]")]
        names = [self._names[k] for k in keys if k and self._names[k] in visible] \
            + (self._picks(group) if picks else [])
        for up in sorted(self._open_servers(conv)):
            names += [n for n, key in visible.items() if key[0] == up]
        return list(dict.fromkeys(names))

    @staticmethod
    def _cap(group) -> int:
        """How many learned tools this group lists: its own setting, else the measured default."""
        try:
            cap = group["adaptive_cap"]
        except (KeyError, IndexError):       # the minimal fallback group has no such field
            cap = None
        return int(cap) if cap else adaptive.CAP

    def recap(self):
        """A group's cap or defaults changed, or a key moved: every key picks again under its
        group's current rule. A few hundred multiplications per key."""
        for r in self.db.execute("SELECT k.id, g.defaults, g.adaptive_cap, g.servers FROM keys k "
                                 "JOIN groups g ON g.id = k.group_id").fetchall():
            self.adaptive.recap(r["id"], self._no_slot(r), self._cap(r))

    def _no_slot(self, group) -> set:
        """Tools that never take a learned slot: the group's defaults, listed already, and every
        tool on a server it does not reach."""
        out = {k for k in (self.resolve(d) for d in json.loads(group["defaults"] or "[]")) if k}
        chosen = self._chosen(group)
        return out if chosen is None else out | {k for k in self._names if k[0] not in chosen}

    def _picks(self, group, key_id=None) -> list[str]:
        """The learned tools of the calling key (or the one named), by the names clients call
        them -- in an adaptive group only. A tool whose server is gone just drops out."""
        if group["mode"] != "adaptive":
            return []
        key_id = gate.current_key.get() if key_id is None else key_id
        return [self._names[t] for t in self.adaptive.picks(key_id, self._no_slot(group))
                if t in self._names]

    def learn(self):
        """Relearn every key's habits from the events log -- the only memory there is, so a
        restart knows exactly what it knew. The last HISTORY calls per key: older ones no
        longer weigh anything."""
        groups = {r["id"]: r for r in self.db.execute(
            "SELECT k.id, g.defaults, g.adaptive_cap, g.servers FROM keys k JOIN groups g ON g.id = k.group_id")}
        exclude = {k: self._no_slot(r) for k, r in groups.items()}
        rows = self.db.execute(
            "SELECT key_id, upstream, tool FROM (SELECT key_id, upstream, tool, id, ROW_NUMBER() "
            "OVER (PARTITION BY key_id ORDER BY id DESC) AS n FROM events WHERE key_id IS NOT NULL) "
            "WHERE n <= ? ORDER BY id", (adaptive.HISTORY,)).fetchall()
        self.adaptive.rebuild([tuple(r) for r in rows], lambda k: exclude.get(k, ()),
                              lambda k: self._cap(groups[k]) if k in groups else adaptive.CAP)
        log.info("adaptive: learned %d keys from %d calls", len(self.adaptive.models), len(rows))

    def adaptive_view(self, key_id, group) -> dict:
        """One key's ranking for the settings page: what is listed, and what is next in line."""
        listed, cap, chosen = group["mode"] == "adaptive", self._cap(group), self._chosen(group)
        return {"listed": listed, "cap": cap, "tools": [
            {"upstream": t[0], "tool": t[1], "name": self._names.get(t), "score": score,
             "picked": picked} for t, score, picked in self.adaptive.ranked(key_id, cap + 5)
            if chosen is None or t[0] in chosen]}

    # ---- conversations -------------------------------------------------------

    def _conversation(self, ctx) -> str:
        """Who is talking, as narrowly as the transport lets us say it. A stateful client sends
        Mcp-Session-Id -- one per connection, which for most apps is one per chat. A client
        that sends none is identified by its key, so its chats share what they opened."""
        headers = getattr(getattr(ctx, "request", None), "headers", None)
        sid = headers.get("mcp-session-id") if headers is not None else None
        return f"session:{sid}" if sid else f"key:{gate.current_key.get()}"

    def _open_servers(self, conv) -> set:
        now = _clock()
        return {up for up, (_, used) in self._opened.get(conv, {}).items() if now - used < OPEN_IDLE}

    def _use(self, conv, upstream) -> bool:
        """Record that a conversation used a server. True if that opened it: its first use, or
        the first after an hour idle -- the moment to tell the model what else is in there."""
        now = _clock()
        for c in [c for c, ups in self._opened.items()
                  if all(now - used >= OPEN_IDLE for _, used in ups.values())]:
            del self._opened[c]                      # forget conversations that went quiet
            self._listed.pop(c, None)
            self._picked.pop(c, None)
            self._shown.pop(c, None)
        ups = self._opened.setdefault(conv, {})
        opened, used = ups.get(upstream, (None, -OPEN_IDLE))
        fresh = now - used >= OPEN_IDLE
        ups[upstream] = (now if fresh else opened, now)
        return fresh

    def _in_client_list(self, group, conv) -> set:
        """What this client actually has in its list -- not what we would hand it now. A server
        opened since its last tools/list is callable, but only through call_tool, and the
        advice we give has to name the call the client can actually make."""
        visible = self._visible(group)
        if group["mode"] == "direct":
            return set(visible)
        listed_at = self._listed.get(conv)
        # the defaults, and the learned tools as they were when it last listed -- not as now
        have = set(self.members(group, picks=False)) | set(self._picked.get(conv, ()))
        now = _clock()
        for up, (opened, used) in self._opened.get(conv, {}).items():
            # strictly after: on a tie (the clock ticks every ~15 ms on Windows) assume it has not
            # re-listed -- call_tool advice works either way, a direct call only if it really has
            if listed_at is not None and opened < listed_at and now - used < OPEN_IDLE:
                have |= {n for n, key in visible.items() if key[0] == up}
        return have

    # ---- what the model is told ----------------------------------------------

    def _servers(self, group, compact: bool = False) -> str:
        """One line per server: how many tools are inside, and -- only if an admin wrote one --
        a curated line about what it is for. The server's own instructions are NOT used here:
        they tend to enumerate their own tools, which is exactly what a hybrid group keeps out
        of the context.

        `compact`: only the opening of that line. search_tools' description is sent with every
        request; the instructions are read once, and list_mcpservers has the whole text."""
        lines = []
        for row in self._server_rows(group):
            link = self._links.get(row["name"])
            n = len(link.tools) if link else 0
            state = self._state(link)
            line = f"  - {row['name']}: " + (
                f"{n} tools" if state == "connected" else
                f"{n} tools, connecting" if state == "connecting" else
                (f"{n} tools, " if n else "") + "down right now (the manager keeps retrying)")
            if row["description"]:
                desc = " ".join(row["description"].split())
                line += " - " + (hints.brief(desc, 80) if compact else desc)
            lines.append(line)
        return "\n".join(lines) or "  (none registered yet)"

    def instructions(self) -> str:
        group = self.group()
        return INSTRUCTIONS[group["mode"]].format(servers=self._servers(group))

    def _block(self, name, overrides=None) -> str:
        """One tool as the model sees it in a search: name, server, what it is for, and its
        arguments -- enough to write the call, and no more, because every match costs tokens.
        What it is for is the opening of its description; the required arguments say what they
        take, the optional ones only their type. A call that still gets one wrong is answered
        with the full usage. No score: the order already says which fits best."""
        key = self._exposed[name]
        overrides = self._overrides() if overrides is None else overrides
        down = ", down" if self._state(self._links.get(key[0])) == "down" else ""
        return (f"{name} [{key[0]}{down}] {hints.brief(self.desc(key, overrides), 200)}\n"
                f"  args: {hints.signature(self._defs[key].input_schema, compact_optional=True)}")

    # ---- API reads ----------------------------------------------------------

    def live_view(self, group) -> int:
        return len(self.members(group))

    @staticmethod
    def _state(link) -> str:
        if link is None:
            return "down"
        if link.session is not None:
            return "connected"
        return "connecting" if link.connecting and link.down_since is None else "down"

    def upstreams_state(self):
        out = []
        now = time.monotonic()
        for r in self.db.execute("SELECT * FROM upstreams ORDER BY name"):
            link = self._links.get(r["name"])
            own = (link.instructions or link.title) if link else None
            tools = len(link.tools) if link else 0
            status = "disabled" if not r["enabled"] else self._state(link)
            down = status == "down" and link is not None
            out.append({"name": r["name"], "url": r["url"], "enabled": bool(r["enabled"]),
                        # the admin entered these; the admin token is the boundary, and an
                        # editor that hid the values would force retyping them on every edit
                        "headers": json.loads(r["headers"] or "{}"),
                        "shared": bool(r["shared"]),     # one session per group, not per key
                        "status": status, "connected": status == "connected",
                        "error": link.error if down else None,
                        # how long it has been down, and when the manager tries again (seconds)
                        "down_for": round(now - link.down_since) if down and link.down_since else None,
                        "retry_in": max(0, round(link.next_try - now)) if down and link.next_try else None,
                        "tools": tools,
                        "sessions": self.sessions(r["name"]),   # clients with their own session open
                        # what the model is actually told, and where it came from
                        "description": r["description"] or short(own or "") or f"{tools} tools",
                        "overridden": r["description"] is not None,
                        "server_says": short(own or "")})
        return out

    def all_tools(self):
        """Everything registered, for the settings page, by the name a client would call."""
        overrides = self._overrides()
        return [{"exposed": name, "upstream": key[0], "tool": key[1],
                 "description": self.desc(key, overrides), "overridden": key in overrides,
                 "server_says": self._defs[key].description or "",   # what an edit would revert to
                 "arguments": hints.signature(self._defs[key].input_schema)}
                for name, key in sorted(self._exposed.items())]

    def overrides_state(self):
        return [{"upstream": r["upstream"], "tool": r["tool"], "title": r["title"],
                 "description": r["description"], "updated_at": r["updated_at"]}
                for r in self.db.execute("SELECT * FROM tool_overrides ORDER BY upstream, tool")]

    async def search(self, query, limit=10, page=1, server=None):
        """Ranked, paged hits carrying everything needed to judge them: which MCP it came
        from, the tool, the description the model would be shown, whether that text is
        yours or the server's, its arguments, and the similarity.

        `exposed` stays the one spelling you would actually call -- browser__list_downloads,
        never browser/list_downloads.
        """
        return (await self._search(query, limit, page, server))[0]

    async def _search(self, query, limit=10, page=1, server=None, group=None):
        """search(), plus the query's vector (None when lexical) for callers that score more.
        `group` limits it to what that group reaches; the settings page searches everything."""
        idx = self.index()
        mode, qvec = idx["mode"], None
        if mode == "lexical":
            # The background embedder gives up after a run of failures; a search is the moment
            # someone wants it back, so ask again (a no-op while a pass is already running).
            self._kick_embed()
        if mode == "semantic" and (query or "").strip():
            got = await scoring.embed([query], self.embed_cfg, "query")
            qvec = got[0] if got else None
            if qvec is None:          # the model went away since the index was built
                mode = "lexical"
        reach = self._exposed if group is None else self._visible(group)
        names = {n for n, k in reach.items() if server is None or k[0] == server}
        hits = [h for h in scoring.top_k({**idx, "mode": mode}, query, qvec, names=names) if h[0] in self._exposed]
        limit = max(1, int(limit or 10))
        pages = max(1, -(-len(hits) // limit))
        page = min(max(1, int(page or 1)), pages)
        overrides = self._overrides()
        results = [{"exposed": name, "mcp": self._exposed[name][0], "tool": self._exposed[name][1],
                    "description": self.desc(self._exposed[name], overrides), "score": score,
                    "source": "edited here" if self._exposed[name] in overrides else "mcp server",
                    "available": self._state(self._links.get(self._exposed[name][0])) == "connected",
                    "arguments": hints.signature(self._defs[self._exposed[name]].input_schema)}
                   for name, score in hits[(page - 1) * limit: page * limit]]
        return ({"mode": mode, "query": query, "total": len(hits), "page": page,
                 "page_size": limit, "pages": pages, "results": results}, qvec)

    async def _down_servers_for(self, query, qvec, group) -> list:
        """Servers that are down -- or have never connected, so no tool of theirs is known --
        whose purpose matches the query: [(name, description)]. Each one found is woken, so a
        server someone clearly needs is retried now rather than at the end of its backoff."""
        if not (query or "").strip():
            return []
        rows = self._server_rows(group)
        qt = scoring._tokens(query)
        picks = []
        if qvec is not None:
            # By meaning, among ALL servers: the best one is the place for this only if it clearly
            # is -- above a floor AND well ahead of the next (the measurements are in scoring.py).
            docs = {r["name"]: f"{r['name']}\n{r['description'] or ''}".strip() for r in rows}
            missing = [d for d in docs.values() if d not in self._vectors]
            if missing:
                got = await scoring.embed(missing, self.embed_cfg)
                for d, v in zip(missing, got or []):
                    self._vectors[d] = scoring.normalise(v)
            q = scoring.normalise(qvec)
            scored = sorted(((sum(a * b for a, b in zip(q, self._vectors[d])), name)
                             for name, d in docs.items() if self._vectors.get(d) and len(self._vectors[d]) == len(q)),
                            reverse=True)
            if scored and scored[0][0] >= scoring.SERVER_FLOOR and (
                    len(scored) == 1 or scored[0][0] - scored[1][0] >= scoring.SERVER_LEAD):
                picks = [scored[0][1]]
        else:
            # By words: a third of the query's words name the server or its purpose
            for r in rows:
                words = scoring._tokens(f"{r['name']} {r['description'] or ''}") | \
                        scoring._tokens(r["name"].replace("_", " "))
                if len(qt & words) / max(len(qt), 1) >= 0.34:
                    picks.append(r["name"])
        out = []
        for name in picks:
            link = self._links.get(name)
            if self._state(link) == "connected":
                continue                    # it is up: its tools answer for themselves
            out.append((name, next(r["description"] for r in rows if r["name"] == name)))
            if link is not None:
                await self._revive(link, wait=0)
        return out

    def _down_line(self, servers) -> str:
        return "\n".join(
            f"{name}{f' ({short(desc, 120)})' if desc else ''} looks like the place for this, but it "
            f"is down right now. The manager is reconnecting it now -- try again in a moment."
            for name, desc in servers)

    def stats(self):
        """Usage for the settings page: the most-called tools, and the per-day call counts of
        the last 90 days for the volume chart. Days with no calls are absent on purpose --
        the page draws the line and fills the gaps, so the answer stays small on a quiet
        manager. ts is UTC ISO, which SQLite's date() and a string compare both read."""
        tools = [dict(r) for r in self.db.execute(
            "SELECT upstream, tool, COUNT(*) AS calls, MAX(ts) AS last_seen FROM events "
            "GROUP BY upstream, tool ORDER BY calls DESC LIMIT 50")]
        days = [dict(r) for r in self.db.execute(
            "SELECT date(ts) AS day, COUNT(*) AS calls, SUM(status != 'ok') AS failed FROM events "
            "WHERE ts >= date('now', '-90 days') GROUP BY day ORDER BY day")]
        return {"tools": tools, "days": days}

    def stats_detail(self, days: int = 30) -> dict:
        """The statistics page, over the last `days` days: totals and outcomes, per tool and per
        server (calls, failures, latency p50/p95), per app (key), calls by weekday and hour, the
        tools most often called one after the other, and each key's adaptive ranking.

        A separate call from stats(): the overview polls that one, and this one reads every
        event in the window. Hours are UTC -- the page moves them to its own clock. Latency
        percentiles are nearest-rank over the window, failures included: a timeout is the
        slowest answer there is."""
        rows = self.db.execute(
            "SELECT ts, key_id, client_id, upstream, tool, latency_ms, status FROM events "
            "WHERE ts >= date('now', ?) ORDER BY id", (f"-{days} days",)).fetchall()
        keys = {r["id"]: r for r in self.db.execute(
            "SELECT k.id, k.label, k.revoked, g.name AS grp, g.mode, g.defaults, g.adaptive_cap, g.servers "
            "FROM keys k JOIN groups g ON g.id = k.group_id")}

        def pct(vals, q):
            vals = sorted(v for v in vals if v is not None)
            return round(vals[min(len(vals) - 1, int(q * len(vals)))]) if vals else None

        def bucket():
            return {"calls": 0, "failed": 0, "lat": [], "last_seen": None}

        by_tool, by_server, by_key, status = {}, {}, {}, {}
        heat = [[0] * 24 for _ in range(7)]          # [Monday..Sunday][hour], UTC
        chains, prev = {}, {}                        # (from, to) -> n; key -> (tool, when)
        for r in rows:
            when = datetime.fromisoformat(r["ts"])
            failed = r["status"] != "ok"
            status[r["status"]] = status.get(r["status"], 0) + 1
            for table, k in ((by_tool, (r["upstream"], r["tool"])), (by_server, r["upstream"]),
                             (by_key, r["key_id"] if r["key_id"] is not None else r["client_id"])):
                b = table.setdefault(k, bucket())
                b["calls"] += 1
                b["failed"] += failed
                b["lat"].append(r["latency_ms"])
                b["last_seen"] = max(b["last_seen"] or "", r["ts"])   # rows are by id, not always by time
            who, tool = r["key_id"] if r["key_id"] is not None else r["client_id"], (r["upstream"], r["tool"])
            used = by_key[who].setdefault("tools", {})
            used[tool] = used.get(tool, 0) + 1
            heat[when.weekday()][when.hour] += 1
            # a chain: the same app's next call within CHAIN_GAP, to a different tool
            before = prev.get(who)
            if before and before[0] != tool and (when - before[1]).total_seconds() <= CHAIN_GAP:
                chains[(before[0], tool)] = chains.get((before[0], tool), 0) + 1
            prev[who] = (tool, when)

        def out(b, **extra):
            return {"calls": b["calls"], "failed": b["failed"], "p50_ms": pct(b["lat"], .5),
                    "p95_ms": pct(b["lat"], .95), "last_seen": b["last_seen"], **extra}

        apps = []
        for k, b in sorted(by_key.items(), key=lambda kv: -kv[1]["calls"]):
            row = keys.get(k)
            top = sorted(b["tools"].items(), key=lambda kv: -kv[1])[:3]
            apps.append(out(b, key_id=row["id"] if row else None,
                            label=row["label"] if row else str(k),
                            group=row["grp"] if row else None, mode=row["mode"] if row else None,
                            revoked=bool(row["revoked"]) if row else None,
                            tools_used=len(b["tools"]),
                            top=[{"upstream": t[0], "tool": t[1], "calls": n} for t, n in top],
                            adaptive=self.adaptive_view(row["id"], row)["tools"] if row else []))
        return {
            "days": days, "calls": len(rows), "status": status,
            "p50_ms": pct([r["latency_ms"] for r in rows], .5),
            "p95_ms": pct([r["latency_ms"] for r in rows], .95),
            "tools": [out(b, upstream=k[0], tool=k[1], name=self._names.get(k))
                      for k, b in sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])],
            "servers": [out(b, upstream=k) for k, b in sorted(by_server.items(), key=lambda kv: -kv[1]["calls"])],
            "apps": apps,
            "heat": heat,
            "chains": [{"from": {"upstream": a[0], "tool": a[1]}, "to": {"upstream": b[0], "tool": b[1]},
                        "count": n} for (a, b), n in sorted(chains.items(), key=lambda kv: -kv[1])[:15]],
        }

    # ---- MCP handlers -------------------------------------------------------

    async def _list_tools(self, ctx, _params):
        group = self.group()
        overrides, titles = self._overrides(), self._titles()
        out = []
        conv = self._conversation(ctx)
        now = _clock()
        for c in [c for c, at in self._listed.items() if now - at >= OPEN_IDLE and c not in self._opened]:
            del self._listed[c]
            self._picked.pop(c, None)
        self._listed[conv] = now      # from here on, what it opened is really in its list
        self._picked[conv] = self._picks(group)
        self._heard[conv] = (self._snapshot(group), now)   # and it has just read every server's state
        for name in self.members(group, conv):
            key = self._exposed[name]
            t, d = self._defs[key], self.desc(key, overrides)
            title = titles.get(key) or t.title
            out.append(t.model_copy(update={"name": name, "description": d, "title": title})
                       if (d != t.description or name != key[1] or title != t.title) else t)
        if group["mode"] in SEARCHING:
            out += native_tools(self._servers(group, compact=True))
        return types.ListToolsResult(tools=out, nextCursor=None)

    async def _call_tool(self, ctx, params):
        """Every call, direct or through call_tool, goes the same way: find the tool however it
        was spelled, fix what has one obvious fix, refuse what the upstream would refuse anyway
        -- with exactly what to send instead -- and say all of it in the one result. Including
        news the model did not ask for but needs: a server went down, came back, or was added.
        """
        conv = self._conversation(ctx)
        result, notes, upstream, door = await self._dispatch(ctx, params, conv)
        news = self._news(conv, self.group(), skip=upstream)
        if news:
            try:            # the server index in the client's descriptions is stale now
                await ctx.session.send_tool_list_changed()
            except Exception as e:  # noqa: BLE001 -- a session that cannot notify is still a session
                log.debug("list_changed not sent: %s", e)
        return self._noted(result, notes + news, door)

    async def _dispatch(self, ctx, params, conv):
        """-> (result, notes, the upstream it was for if any, came through call_tool)."""
        t0 = time.monotonic()
        group = self.group()
        hybrid = group["mode"] in SEARCHING
        name, args, notes = params.name, dict(params.arguments or {}), []
        door = hybrid and name == "call_tool"

        if door:
            # The way in for clients that never re-fetch their list: the target arrives as an
            # argument instead of as the call name. It adds a route, not a permission: the name
            # is looked up among what this group reaches, like any other call.
            name, args, problem = self._unwrap(args, notes)
            if problem:
                return self._err(problem), [], None, door
        native = NATIVE_BY_NORM.get(hints.norm(str(name or ""))) if hybrid else None
        if native == "search_tools":
            return self._text(await self._search_text(args, group)), notes, None, door
        if native == "list_mcpservers":
            return self._text(self._list_mcpservers(group)), notes, None, door
        if native == "get_tools_from_server":
            text, ok = self._server_tools(args, conv, group)
            return (self._text(text) if ok else self._err(text)), notes, None, door

        key, respelled = self._find(name, group)
        if key is None:
            return self._err(await self._did_you_mean(name, group, conv)), notes, None, door
        exposed, tool = self._names[key], self._defs[key]   # before any await: the pool can change
        listed_before = set(self.members(group, conv))
        via = exposed not in self._in_client_list(group, conv)   # must it go through call_tool?
        if respelled:
            notes.append(f'There is no tool "{name}"; I ran {exposed}, which is the one you meant. '
                         "Use that exact name next time.")

        args, fixes, problems, warnings = hints.check(tool.input_schema, args)
        if problems:
            # refused here, before the upstream: it would refuse too, less helpfully
            self._log(group, key, t0, "invalid")
            return self._err(
                f"{exposed} did not run: its arguments need fixing.\n"
                # what would have been corrected too: the next call has to be right in one go
                + "\n".join([f"- {p}" for p in problems] + [f"- also: {f}" for f in fixes + warnings]) + "\n\n"
                + hints.usage(exposed, tool.input_schema, via)), notes, key[0], door
        if fixes:
            notes.append("I corrected your call so it could run: " + "; ".join(fixes)
                         + ". Send it that way next time.")
        notes += warnings

        link = self._links.get(key[0])
        if link is not None and link.session is None:
            await self._revive(link)            # it may have just come back: then the call works
        if link is None or link.session is None:
            self._log(group, key, t0, "down")
            notes += await self._alternatives(key, exposed, group)
            return self._err(self._down(link, key[0], exposed)), notes, key[0], door

        result, reachable = await self._send(link, key, args, exposed, notes)
        if not reachable:
            self._log(group, key, t0, "down")
            notes += await self._alternatives(key, exposed, group)
            return self._err(self._down(link, key[0], exposed)), notes, key[0], door
        if result.is_error:
            text = _text_of(result)
            if via or ARG_ERROR.search(text):
                # the model never saw this schema, or the error is about the call's shape:
                # the usage is the answer, and the retry can be right first time
                notes.append(hints.usage(exposed, tool.input_schema, via))
            if FETCH_ERROR.search(text):
                notes += await self._alternatives(key, exposed, group)
        if hybrid and self._use(conv, key[0]):
            seen = self._shown.get(conv, {}).get(key[0])
            if seen is not None and _clock() - seen < OPEN_IDLE:
                notes.append(f"{key[0]}'s tools, which you listed with get_tools_from_server, are open to "
                             "you now: call any of them by name (through call_tool if it is not in your list).")
            else:
                opened = self._catalogue(key[0], listed_before | {exposed}, exposed)
                if opened:
                    notes.append(opened)
            try:            # clients that listen re-list and get real entries
                await ctx.session.send_tool_list_changed()
            except Exception as e:  # noqa: BLE001 -- a session that cannot notify is still a session
                log.debug("list_changed not sent: %s", e)
        self._log(group, key, t0, "error" if result.is_error else "ok")
        return result, notes, key[0], door

    async def _send(self, link, key, args, exposed, notes):
        """-> (result, reachable). Runs the call on this client's own session to the server, and
        watches the server while it runs.

        A server that dies mid-session does not fail the call: the SDK's writer dies with it and
        the call waits out its whole limit. So a call still running after two seconds gets the
        host checked every five -- a long job on a live server is left alone (a busy server
        still takes connections), a dead one is reported as down within seconds.

        A server that RESTARTS passes that check within seconds, but every session it had is gone,
        and a call that was running on one can hang on a connection that never closes (a replaced
        container's). So the call's own session is asked too (`_forgot`). Once it is gone:
          - a call the server turned away at once never ran: it runs again on a fresh session;
          - a call cut off mid-run runs again only if its tool is readOnlyHint -- the server's own
            word that running it twice changes nothing; any other is reported, never repeated;
        and either way the model is told that what the server kept for it is gone.
        """
        from mcp.shared.exceptions import MCPError
        owner = self._owner(link)
        read_only = bool(getattr(getattr(self._defs.get(key), "annotations", None), "read_only_hint", None))
        for attempt in (1, 2):
            cl = await self._client(owner, link)
            if cl.session is None:
                if not await _reachable(link.url):
                    self._lost(link, "stopped answering")
                    return None, False
                return self._err(f"Could not open a session on {key[0]} for this client "
                                 f"({cl.error or 'it did not answer in time'}). Try again in a moment."), True
            cl.busy += 1
            sent = time.monotonic()
            call = asyncio.ensure_future(cl.session.call_tool(key[1], args, read_timeout_seconds=CALL_TIMEOUT))
            wait, cut = 2, None             # cut: the call's session died under it
            try:
                while True:
                    done, _ = await asyncio.wait({call}, timeout=wait)
                    if done:
                        return call.result(), True
                    if not await _reachable(link.url):
                        call.cancel()
                        self._lost(link, "stopped answering")
                        return None, False
                    if await self._forgot(cl):
                        cut = "restarted"
                        break
                    wait = 5
            except Exception as e:  # noqa: BLE001 -- name the upstream and the tool, always
                up = await _reachable(link.url)
                if up and (_session_gone(e) or await self._forgot(cl)):
                    cut = e
                elif _transport_failure(e) and not up:
                    self._lost(link, _reason(e))
                    return None, False
                elif isinstance(e, MCPError) and e.code == types.REQUEST_TIMEOUT:
                    return self._err(
                        f"{exposed} did not finish within {CALL_TIMEOUT // 60} minutes. {key[0]} is still up, "
                        "so it may still be running there: if the server has a status or list tool, check "
                        "it before trying again, or a retry may start the job twice."), True
                else:
                    link.wake.set()     # its owner checks the connection now, not in half a minute
                    return self._err(f"{key[0]} / {key[1]} failed: {_reason(e)}"), True
            finally:
                cl.busy -= 1
                cl.last_used = time.monotonic()
                if not call.done():
                    call.cancel()
            # The server forgot this client's session: it restarted.
            self.close_clients(upstream=key[0], owner=owner, now=True)
            link.wake.set()                 # its shared session is stale too
            refused = _session_gone(cut) and time.monotonic() - sent < 1     # turned away: it never ran
            reset = (f"{key[0]} restarted, so anything it kept for this client from earlier calls "
                     f"(open pages, logins, jobs, handles) is gone")
            if attempt == 1 and (refused or read_only):
                notes.append(f"{reset}. " + ("This call ran on a fresh session" if refused else
                             f"{exposed} was cut off by the restart; it changes nothing, so it ran again "
                             "on a fresh session") + f", and its result shows {key[0]} as it is now, just "
                             "restarted -- not what you had there before.")
                continue
            return self._err(
                f"{key[0]} restarted while {exposed} was running, so the call was cut off -- it may or may "
                f"not have taken effect there. Check before running it again (with a status or list "
                f"tool, if {key[0]} has one). {reset}; the next call gets a fresh session."), True
        return self._err(f"{key[0]} / {key[1]} failed: the server kept forgetting this client's session"), True

    async def _forgot(self, cl) -> bool:
        """Has the server forgotten this client's session -- did it restart? A restarted server takes
        connections like a live one, so only the session itself can say. Asked with a ping: a
        server that no longer knows the session answers that at once. No answer in time is not
        "forgotten" -- a busy server is still alive."""
        from mcp.shared.exceptions import MCPError
        session = cl.session
        if session is None or cl.stop.is_set():
            return True
        try:
            with anyio.fail_after(5):
                await session.send_ping()
        except TimeoutError:
            return False
        except MCPError as e:
            return _session_gone(e) or e.code == types.CONNECTION_CLOSED
        except Exception:  # noqa: BLE001 -- its transport is broken: no call can finish on it
            return True
        return False

    async def _alternatives(self, key, exposed, group) -> list:
        """When a tool fails at its job, name the tools on OTHER servers that do the same job --
        one line, and the retry goes to the right place. (web_url_read blocked by a site, say,
        while the browser reads the same page fine.)"""
        desc = self.desc(key)
        query = re.split(r"(?<=[.!?])\s", desc, 1)[0][:200] if desc else key[1].replace("_", " ")
        # Scored among the OTHER servers only: word-match scores are relative to the best hit,
        # and the best hit for a tool's own description is always the tool itself.
        visible = self._visible(group)
        others = {n: d for n, d in self.documents().items() if n in visible and self._exposed[n][0] != key[0]
                  and self._state(self._links.get(self._exposed[n][0])) == "connected"}
        if not others:
            return []
        sub = scoring.build(others, self._vectors)
        qvec = None
        if sub["mode"] == "semantic":
            got = await scoring.embed([query], self.embed_cfg, "query")
            qvec = got[0] if got else None
        hits = scoring.top_k(sub if qvec else {**sub, "mode": "lexical"}, query, qvec)
        if not qvec:            # relative scores say nothing absolute: ask for two shared words
            qt = scoring._tokens(query)
            hits = [h for h in hits if len(qt & sub["docs"][h[0]]) >= 2]
        if not hits:
            return []
        overrides = self._overrides()
        return [f"If {exposed} keeps failing, another server can do the same job:\n" + "\n".join(
            f"- {n} [{self._exposed[n][0]}] — {short(self.desc(self._exposed[n], overrides), 120)}\n"
            f"  arguments: {hints.signature(self._defs[self._exposed[n]].input_schema)}"
            for n, _ in hits[:2])]

    def _snapshot(self, group) -> dict:
        """{server: up?} for every server this group reaches -- what a conversation is told the
        world is. A server added to the group reads as new, one taken out as removed."""
        return {r["name"]: self._state(self._links.get(r["name"])) == "connected"
                for r in self._server_rows(group)}

    def _news(self, conv, group, skip=None) -> list:
        """What changed among the servers since this conversation last heard: one went down,
        came back, or was added. Said once, in whatever result goes out next -- the server list
        in the client's descriptions was frozen when it connected, and most clients never
        re-read it. The server the call itself was for is skipped: its own answer says it."""
        now = _clock()
        for c in [c for c, (_, at) in self._heard.items() if now - at >= OPEN_IDLE]:
            del self._heard[c]
        current = self._snapshot(group)
        seen = self._heard.get(conv, (None, now))[0]
        self._heard[conv] = (current, now)
        if seen is None:
            return []
        descs = {r["name"]: r["description"] for r in self.db.execute("SELECT name, description FROM upstreams")}
        out = []
        for up, is_up in sorted(current.items()):
            if up == skip or seen.get(up) == is_up:
                continue
            n = len(self._links[up].tools) if up in self._links else 0
            what = f" - {short(descs.get(up), 120)}" if descs.get(up) else ""
            if up not in seen:
                out.append(f"New server: {up} ({n} tools){what}. search_tools finds its tools."
                           if is_up else f"New server: {up}{what}. It is not connected yet.")
            elif is_up:
                out.append(f"{up} is up again: its {n} tools work now{what}.")
            else:
                out.append(f"Heads-up: {up} just went down. The manager keeps trying to reconnect; "
                           "its tools work again as soon as it is back.")
        out += [f"{up} was removed." for up in sorted(set(seen) - set(current)) if up != skip]
        return out

    def _log(self, group, key, t0, status):
        # logged against the REAL tool, never against call_tool
        key_id = gate.current_key.get()
        db.log_event(self.db, gate.current_client.get(), group["id"], key[0], key[1],
                     round((time.monotonic() - t0) * 1000, 1), status, key_id)
        # Every call teaches its key's ranking, whatever the mode: switching a group to
        # adaptive then starts from what its apps already do. A refused or failed call counts
        # too -- it is the tool the model reached for. A few hundred multiplications.
        self.adaptive.observe(key_id, key, self._no_slot(group), self._cap(group))

    def _unwrap(self, args, notes):
        """call_tool's own arguments -> (tool name, its arguments, problem). Accepts what models
        actually send -- tool/args/params for name/arguments, or the tool's arguments flattened
        next to the name -- and says how it read them."""
        nk = next((k for k in NAME_KEYS if isinstance(args.get(k), str) and args[k].strip()), None)
        if nk is None:
            return None, None, ('call_tool needs the name of the tool to run: call_tool '
                                '{"name": "<tool>", "arguments": {...}}. search_tools finds names.')
        target = args[nk].strip()
        ak = next((k for k in ARG_KEYS if k in args), None)
        if ak is not None:
            inner = args[ak]
        else:
            inner = {k: v for k, v in args.items() if k != nk}
            if inner:
                notes.append(f'A tool\'s own arguments go inside "arguments": call_tool {{"name": '
                             f'"{target}", "arguments": {{...}}}}. I moved {", ".join(inner)} there.')
        if isinstance(inner, str):      # weaker models send the object as a string
            try:
                inner = json.loads(inner) if inner.strip() else {}
            except ValueError:
                return None, None, ('"arguments" must be a JSON object of the tool\'s own arguments, '
                                    f'e.g. call_tool {{"name": "{target}", "arguments": {{}}}}')
        if inner is None:
            inner = {}
        if not isinstance(inner, dict):
            return None, None, '"arguments" must be a JSON object of the tool\'s own arguments'
        if nk != "name" or ak not in (None, "arguments"):
            notes.append('call_tool takes {"name": ..., "arguments": {...}}; I read your call that way.')
        return target, inner, None

    def _find(self, name, group):
        """(key, respelled) for whatever the model called the tool, or (None, False).

        The same name spelt differently -- listTabs, List-Tab, browser.ListTabs -- runs, because
        there is only one thing it can mean. A real typo runs only if the tool declares it
        changes nothing (readOnlyHint): a guess is fine for a read, not for a delete. Only tools
        this group reaches count: a name on any other server is no tool at all here.
        """
        visible = self._visible(group)
        reachable = set(visible.values())
        key = self.resolve(name)
        if key is not None:
            return (key, False) if key in reachable else (None, False)
        if not isinstance(name, str) or not hints.norm(name):
            return None, False
        n = hints.norm(name)
        same = {key for exp, key in visible.items() if hints.norm(exp) == n}
        same |= {key for key in reachable if hints.norm(key[0] + key[1]) == n}
        if len(same) == 1:
            return same.pop(), True
        ranked = sorted(((difflib.SequenceMatcher(None, n, hints.norm(exp)).ratio(), exp)
                         for exp in visible), reverse=True)
        if ranked and ranked[0][0] >= 0.88 and (len(ranked) < 2 or ranked[0][0] - ranked[1][0] >= 0.05):
            key = self._exposed[ranked[0][1]]
            if getattr(getattr(self._defs[key], "annotations", None), "read_only_hint", None):
                return key, True
        return None, False

    async def _did_you_mean(self, name, group, conv) -> str:
        """A wrong name, answered with the one it most likely meant -- and how to call it, so
        the next call is the right one. If the name sounds like a server that is down (or has
        never connected, so none of its tools are known yet), that is said first."""
        listed = self._in_client_list(group, conv)
        hybrid = group["mode"] in SEARCHING
        visible = self._visible(group)
        n = hints.norm(str(name or ""))
        twins = [e for e, key in visible.items() if n and hints.norm(key[1]) == n]
        if len(twins) > 1:
            return (f'More than one server has a tool called "{name}". Name the one you mean:\n\n'
                    + "\n\n".join(hints.usage(e, self._defs[self._exposed[e]].input_schema,
                                              hybrid and e not in listed) for e in twins))
        by_norm = {hints.norm(e): e for e in visible}
        # A name that is part of a real one is the strongest hint there is: web_search is
        # searxng_web_search with the prefix dropped -- spelling distance would pick `search`.
        inside = sorted((e for c, e in by_norm.items()
                         if min(len(n), len(c)) >= 4 and (n in c or c in n) and c != n),
                        # a tool that contains the whole guess beats one the guess contains
                        key=lambda e: (n not in hints.norm(e), abs(len(hints.norm(e)) - len(n))))
        close = [by_norm[c] for c in difflib.get_close_matches(n, by_norm, 3, 0.6)]
        # Only a real misspelling earns "did you mean". translate_text is 0.6 like get_transcript
        # by its letters, and offering that with its usage invited a call that could never help.
        meant = list(dict.fromkeys(inside[:2] + [by_norm[c] for c in difflib.get_close_matches(n, by_norm, 3, 0.8)]))
        words = re.sub(r"[_\W]+", " ", str(name or "")).strip()
        found, qvec = await self._search(words, 3, group=group) if words else ({"results": []}, None)
        down = await self._down_servers_for(words, qvec, group)
        hits = [h["exposed"] for h in found["results"] if h["exposed"] in self._exposed]
        lead = f'There is no tool called "{name}".'
        if down:
            lead += " " + self._down_line(down)
        overrides = self._overrides()
        if not meant:
            near = list(dict.fromkeys(hits + close))[:3]
            if not near:
                return lead + (" Describe what you want to do to search_tools and it will find the tool."
                               if hybrid and not down else "")
            return (lead + " No tool has a name like it. These come closest in what they do -- check "
                    "that one really does the job before calling it, as none may:\n" + "\n".join(
                        f"- {hints.compact(p, self._defs[self._exposed[p]].input_schema)} ({self._exposed[p][0]})"
                        f" — {short(self.desc(self._exposed[p], overrides), 100)}" for p in near))
        picks = list(dict.fromkeys(meant + close + hits))[:3]
        best = picks[0]
        key = self._exposed[best]
        text = (f'{lead} Did you mean {best} ({key[0]})? {short(self.desc(key, overrides), 200)}\n\n'
                + hints.usage(best, self._defs[key].input_schema, hybrid and best not in listed))
        if picks[1:]:
            text += "\n\nOther close matches:\n" + "\n".join(
                f"- {p} ({self._exposed[p][0]}): {short(self.desc(self._exposed[p], overrides), 100)}"
                for p in picks[1:])
        return text

    def _related(self, name, candidates) -> list:
        """candidates, closest in meaning to `name` first: cosine when the index has vectors,
        shared words otherwise."""
        idx = self.index()
        vec = idx["vectors"].get(name)
        if idx["mode"] == "semantic" and vec:
            score = {c: sum(a * b for a, b in zip(vec, idx["vectors"][c]))
                     for c in candidates if idx["vectors"].get(c)}
        else:
            mine = idx["docs"].get(name, set())
            score = {c: len(mine & idx["docs"].get(c, set())) / (len(idx["docs"].get(c, set())) ** 0.5 or 1)
                     for c in candidates}
        return sorted(candidates, key=lambda c: -score.get(c, 0))

    def _catalogue(self, upstream, already, used) -> str:
        """The rest of a server, for the result of its first use -- so the model never has to
        search that server again in this conversation. In full for a small server; for a big one,
        the ten closest to what was just used in full, and the rest by name: listing a browser's
        57 tools in full spent 1,800 tokens of a news-reading task on captcha solvers."""
        overrides = self._overrides()
        names = sorted(n for n, key in self._exposed.items() if key[0] == upstream and n not in already)
        if not names:
            return ""
        full = names if len(names) <= 12 else self._related(used, names)[:10]
        rest = sorted(set(names) - set(full))
        line = lambda n: (f"- {hints.compact(n, self._defs[self._exposed[n]].input_schema)} — "
                          f"{short(self.desc(self._exposed[n], overrides), 90)}")
        text = (f"You just used {upstream}. All {len(names)} of its other tools are open to you now; "
                "call any of them by name (through call_tool if it is not in your tool list), no search "
                "needed" + (":\n" if not rest else ". Closest to what you just did:\n")
                + "\n".join(line(n) for n in full))
        if rest:
            text += (f"\nAlso on {upstream}: {', '.join(rest)}. A call with the wrong arguments is "
                     "answered with the right ones.")
        return text

    async def _search_text(self, args, group) -> str:
        query = next((args[k] for k in QUERY_KEYS if isinstance(args.get(k), str)), "")
        try:
            size, page = int(args.get("page_size") or SEARCH_PAGE), int(args.get("page") or 1)
        except (TypeError, ValueError):
            size, page = SEARCH_PAGE, 1
        server, problem = self._server_arg(args, group)
        if problem:
            return problem
        found, qvec = await self._search(query, size, page, server, group)
        # A down server is said once, at the top -- not on every one of its matches.
        lead, named = [], set()
        if not server:
            likely = await self._down_servers_for(query, qvec, group)   # down, and the place for this
            if likely:
                lead.append(self._down_line(likely))
                named = {s for s, _ in likely}
        listed = {h["mcp"] for h in found["results"]} | ({server} if server else set())
        lead += [f"{s} is down right now (the manager keeps retrying): its tools will not run until it is back."
                 for s in sorted(listed - named) if self._state(self._links.get(s)) == "down"]
        where = f" on {server}" if server else ""
        if not found["results"]:
            return "\n\n".join(lead + [f"No tools{where} match that. Try other words for the same step, or "
                                       + ("leave out `server` to search them all." if server
                                          else "an empty query to list everything.")])
        overrides = self._overrides()
        head = (f'{found["total"]} matches for "{query}"{where}' if query.strip()
                else f'{found["total"]} tools{where or " in all"}')
        if found["pages"] > 1:
            head += f' (page {found["page"]} of {found["pages"]})'
        body = "\n".join(self._block(h["exposed"], overrides) for h in found["results"])
        top = found["results"][0]["exposed"]
        tail = "Run one with call_tool, e.g. " + hints.example(
            top, self._defs[self._exposed[top]].input_schema, True)
        if found["page"] < found["pages"]:
            tail += f'\nMore: search_tools again with page={found["page"] + 1}.'
        return "\n\n".join(lead + [f"{head}:\n{body}", tail])

    def _server_named(self, given, group):
        """(server, None) for whatever the model called a server, or (None, what to send instead).
        Spellings resolve the way tool names do -- case, separators, a plural -- and so does a
        part of the name that only one server has: "browser" is secure_browser."""
        names = [r["name"] for r in self._server_rows(group)]
        g = given.strip() if isinstance(given, str) else ""
        n = hints.norm(g)
        by_norm = {hints.norm(s): s for s in names}
        for found in ([s for s in names if s == g],
                      [by_norm[n]] if n in by_norm else [],
                      [s for s in names if len(n) >= 3 and n in hints.norm(s)],
                      [by_norm[c] for c in difflib.get_close_matches(n, list(by_norm), 1, 0.8)]):
            if len(found) == 1:
                return found[0], None
        return None, f'There is no server called "{given}". The servers are: {", ".join(names) or "none"}.'

    def _server_arg(self, args, group):
        """The server a call names, if it names one: (server, None), (None, None) or (None, problem)."""
        given = next((args[k] for k in SERVER_KEYS if isinstance(args.get(k), str) and args[k].strip()), None)
        return (None, None) if given is None else self._server_named(given, group)

    def _list_mcpservers(self, group) -> str:
        """{server: {description, tools, status}} -- live, unlike the index in a client's tool
        list, which is as old as its last tools/list. The description is the admin's when one is
        set, the server's own otherwise."""
        lines = []
        for row in self._server_rows(group):
            link = self._links.get(row["name"])
            own = (link.instructions or link.title) if link else None
            state = self._state(link)
            entry = {"description": " ".join((row["description"] or "").split()) or hints.brief(own or "", 200),
                     "tools": len(link.tools) if link else 0,
                     "status": "up" if state == "connected" else state}
            if not entry["description"]:
                del entry["description"]
            lines.append(f" {json.dumps(row['name'])}: {json.dumps(entry, ensure_ascii=False)}")
        return "{\n" + ",\n".join(lines) + "\n}" if lines else "{}"

    def _server_tools(self, args, conv, group) -> tuple[str, bool]:
        """(text, ok). Every tool on one server, as {name: "what it does (args: a, b?)"}: an
        overview to choose from, at about a fifth of the size of search results for the same tools.
        Remembered for this conversation, so its first call there does not list them all again."""
        server, problem = self._server_arg(args, group)
        if server is None:
            return problem or ("get_tools_from_server needs a server: call it with {\"server\": \"<name>\"}. "
                               + self._server_named("", group)[1].split(". ", 1)[1]), False
        overrides = self._overrides()
        state = self._state(self._links.get(server))
        tools = [f" {json.dumps(n)}: "
                 + json.dumps(f"{hints.brief(self.desc(k, overrides), 110)} (args: "
                              f"{hints.arg_names(self._defs[k].input_schema)})", ensure_ascii=False)
                 for n, k in sorted(self._exposed.items()) if k[0] == server]
        self._shown.setdefault(conv, {})[server] = _clock()
        head = {"server": server, "status": "up" if state == "connected" else state}
        if not tools:
            head["note"] = "it has never connected, so its tools are not known yet; the manager keeps trying"
        return json.dumps(head)[:-1] + ', "tools": {' + ("\n" + ",\n".join(tools) + "\n" if tools else "") + "}}", True

    def _noted(self, result, notes, door=False):
        """The manager's notes ride in the same result, after whatever the tool said: one
        message, so the model learns the fix without spending a turn on it.

        Through call_tool the structured copy of a result is dropped and its JSON kept as text.
        Clients that see structuredContent show the model only that -- every note in `content`
        vanished. call_tool declares no outputSchema, so nothing requires the structured copy."""
        if door and getattr(result, "structured_content", None) is not None:
            content = list(result.content)
            if not any(getattr(c, "type", "") == "text" for c in content):
                content.insert(0, types.TextContent(type="text", text=json.dumps(result.structured_content, indent=2)))
            result = result.model_copy(update={"content": content, "structured_content": None})
        if not notes:
            return result
        note = types.TextContent(type="text", text="[mcp-manager] " + "\n\n".join(notes))
        return result.model_copy(update={"content": list(result.content) + [note]})

    def _text(self, text):
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)

    def _err(self, msg):
        return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=True)


def _transport_failure(e) -> bool:
    """Did the connection fail, rather than the call? An MCPError is the server answering --
    unless it is the transport's own: closed, timed out, or a session the server forgot."""
    from mcp.shared.exceptions import MCPError
    if not isinstance(e, MCPError):
        return True
    return e.code in (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT) or _session_gone(e)


# how servers say they no longer know a session -- after a restart, typically
SESSION_GONE = re.compile(r"session (terminated|not found|expired)|unknown session", re.I)


def _session_gone(e) -> bool:
    from mcp.shared.exceptions import MCPError
    return isinstance(e, MCPError) and bool(SESSION_GONE.search(str(e)))


def _text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)
