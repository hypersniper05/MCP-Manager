# MCP-Manager — project notes

One MCP server in front of many. Upstream MCP servers are registered once in the manager; every app
connects with its own key, and the key's **group** decides the tool view it gets. A settings page
(admin panel) and a management API run on the same port; MCP clients can never reach them.

The README is the user-facing doc (run it, connect an app, the management API). `LLM-GUIDE.md` is
for an LLM or harness integrating with it (install, connect, the tool contract, building an admin page,
every API shape) — generic, nothing about this machine. Update both when the API or the tool
behaviour changes. This file is the inside view: how it works, why, and how to work on it.

## Layout

```
mcp_manager/
  __main__.py   entry: boot config -> DB (seed on first run) -> MCP app + API + portal, gate, CORS
  config.py     boot config + .env loading; DEFAULTS (host 127.0.0.1 — local-only by default); env overrides
                MCPM_ADMIN_TOKEN/MCPM_HOST/MCPM_DB/MCPM_MODELS_DIR/MCPM_TLS_CERT/MCPM_TLS_KEY;
                tls_kwargs() = uvicorn's ssl options, both files or nothing
  gate.py       the ONLY auth check: /api/* needs the admin token, everything else a registered key.
                Sets contextvars current_client / current_group / current_key for the handlers.
  gateway.py    the core (~1400 lines): upstream links, per-key client sessions, tool views,
                search, call routing, coaching, outage handling, news
  hints.py      argument checking/fixing and usage text (pure functions)
  scoring.py    search index: document text, prompts, semantic + lexical ranking, floors
  adaptive.py   per-key tool ranking for adaptive groups (pure; the events log is its memory)
  embedder.py   embeddinggemma-300m (4-bit ONNX) on onnxruntime + tokenizers, in-process; downloads it
  db.py         SQLite schema, migrations, seed, small helpers
  api.py        management API (Starlette routes, mounted on the MCP app)
  tls.py        makes the HTTPS certificate: python -m mcp_manager.tls (cryptography, no openssl)
portal/index.html   the admin panel: one file, no build step, a pure client of the API
tests/              pytest suite + spiketools.py (a scriptable test upstream)
config.yaml         boot config + first-run seed
install.sh          Linux + macOS installer; install.bat only launches scripts/install.ps1
```

## How it works

**Groups have three modes.** `direct`: every tool from every connected server is in the
list. `adaptive`: hybrid plus up to 10 learned tools per key (below). `hybrid`: the group's `defaults`, plus the manager's own four: `search_tools`, `call_tool`,
`list_mcpservers`, `get_tools_from_server` (direct gets none: it already lists everything). The mode is a view: a
hybrid key reaches every tool of its group's servers through `call_tool`.

**A group may be limited to chosen servers** (`groups.servers`: NULL = all, new ones too; else a JSON
list of names, `[]` reaches nothing). This one IS a boundary: to the group's keys a server left out
does not exist. Anything a client can list, find or call goes through `_visible(group)` /
`_server_rows(group)`: list, search, the server index, `_find` (no spelling or typo lands outside),
did-you-mean, alternatives, news. Only the admin's own search and tool list see everything. A
default on a left-out server is kept, unlisted, and reported in `unresolved`; a learned tool can't
hold a slot (`_no_slot`), but its habit is remembered, so it returns with the server.
`close_unreachable()` closes sessions the moment access ends (a server taken out, a key moved).
Names stay global: a clash with a server outside the group still prefixes the tool, so a name
never depends on which group is asking. `db.server_moved` follows a rename and a removal.

**Hybrid never depends on the client re-fetching its tool list**, because most clients don't (see
Research). `call_tool(name, arguments)` is the primary path, not an escape hatch:
- `search_tools(query, server?)` returns 5 matches per page, each with its argument signature and a
  call to copy, so search -> call is two round trips, never three. An empty query lists everything.
  `server` limits it to one server and ranks WITHIN it (`top_k(names=)`): the floors are relative to
  the best match, and the global best would cut away the asked-for server's matches.
- Token budget, measured on the live catalogue: a match is the description's opening (whole
  sentences up to 200 chars, `hints.brief`), what each REQUIRED argument takes (60 chars), the type
  of each optional one, no score; a down server is said once at the top. A wrong call still gets
  the full usage. The index in `search_tools`' description carries only each admin description's
  opening (80 chars) -- it rides along with every request; the instructions keep it whole.
- `list_mcpservers`: live `{server: {description, tools, status}}` (admin description, else the
  server's own). `get_tools_from_server`: `{server, status, tools: {name: "brief (args: a, b?)"}}`,
  ~1/5 the size of search blocks; the conversation remembers it, so the server's first use gives a
  one-line reminder instead of the catalogue again. Server names resolve loosely (`_server_named`:
  exact, normalised, unique substring, close match); so do the native tool names (`NATIVE_BY_NORM`).
- The server index (name, tool count, description, down state) is in the instructions AND in
  `search_tools`' own description, because many clients never show instructions to the model.
- **A server opens on first use**: the first result from a server lists all its other tools (in full if
  <= 12; else the 10 closest to the one just used, plus the rest by name), the tools join that
  conversation's `tools/list`, and `list_changed` is sent. Open for an hour after last use (OPEN_IDLE).
- A "conversation" is the `Mcp-Session-Id` if the client sends one, else the key. Claude Code and the
  Python SDK on the newer protocol send none, so all chats of one key are one conversation.
- Advice always names the call the client can actually make: a tool opened since the client's last
  `tools/list` is shown as `call_tool {...}` (`_in_client_list`, compared with `perf_counter` — the
  Windows monotonic clock ticks every 15 ms and caused ties).

**Every result is a message we can add to** (`[mcp-manager]` notes after the tool's own output):
- A name spelt differently (`listTabs`, `browser.ListTabs`) runs, with a note giving the exact name.
  A real typo runs only if the tool is `readOnlyHint` (a guess is fine for a read, not a delete).
- Wrong name: "Did you mean X?" with X's usage — only for a real misspelling (the name is inside a real
  one, or >= 80% alike). Otherwise "No tool has a name like it" + the nearest by purpose, with a warning
  that none may fit (`translate_text` must not be offered `get_transcript`).
- Arguments (`hints.check`): case, type coercion (`"1000"` -> 1000), `null` for an optional, near-miss
  keys are fixed and the call runs, with a note. Unknown keys are dropped when the schema is closed,
  sent with a warning otherwise. A missing required argument is refused BEFORE the upstream, with the
  full usage and every other correction listed (the model resends the whole call).
- `call_tool` tolerates `tool`/`args`/`params` and arguments flattened next to the name.
- Upstream errors about arguments (ARG_ERROR) get the usage appended. A tool that fails at its job
  (blocked, 401/403/404, empty, JS-only — FETCH_ERROR) or a down server gets up to two tools on OTHER
  servers that do the same job, with arguments.
- News, once per change per conversation: "X just went down", "X is up again", "New server", "X was
  removed" — plus `list_changed`.
- Through `call_tool` the structured copy of a result is dropped and the JSON kept as text: clients
  that show only `structuredContent` hid the notes. Direct calls keep it (the client would reject the
  result otherwise).

**Upstreams** (`Link`, one task per upstream — anyio cancel scopes are task-bound):
- Streamable HTTP only. Connect/list/health limit `timeout_ms` (30 s default); a tool call gets 10 min.
- A server's optional `headers` (auth) ride on the connection itself: `streamable_http_client`
  takes no headers, so `_auth_client` builds the SDK's own httpx client with them and
  `_close_auth_client` closes it shielded (the reconnect path drops sessions by cancelling).
  Shared Link and client sessions both get them; changing them makes `reconcile` restart the link.
  `POST /api/v1/test-connection` probes a URL without saving it (portal's Test connection).
- Health every 30 s: TCP connect (liveness — a busy server is alive, and unity never answers a bare
  HEAD), then `tools/list`. One slow check is tolerated, two reconnect.
- Every link and client task shares the task group that keeps /mcp alive, so only a cancellation
  may leave one (`_stopped`). A stop landing on a failing request surfaces as the request's error,
  and raising that ended /mcp for every app while the process and the portal still looked healthy.
  Found 2026-11-06 by a fresh clone's test run, in 2 runs of 5; `test_supervisor.py` pins it.
- A dead session is dropped by cancelling its scope (`_lost`): the SDK's polite DELETE to a dead
  server used to hang the reconnect. Backoff 2 s doubling to 60 s; a call or search that needs a down
  server retries it at once (`_revive`). A call still running after 2 s gets the host checked every
  5 s, so a dead server reads as down in seconds, not after the 10-minute limit.
- A server that RESTARTS takes connections again within seconds, so the TCP check passes — but
  every session it had is gone, and a call running on one can hang on a connection that never
  closes (a replaced Docker container's). So a running call also pings its own session every 5 s
  (`_forgot`); "Session not found/terminated" means it restarted. Then: a call refused at once never
  ran → re-run on a fresh session; a call cut off mid-run → re-run once only if the tool is
  `readOnlyHint`, otherwise "restarted while running, may or may not have taken effect, check first";
  either way the model is told that what the server kept for it (tabs, logins, jobs) is gone. Was a
  10-minute hang; `test_outage.py` reproduces both the
  clean-close and the stranded-connection case.
- Tool lists persist in the DB (`upstreams.tools`), so a down server's tools stay listed and
  searchable, and calling one says "X is down right now; the manager keeps trying to reconnect".
  Search also points at a down server whose name/description matches the query.

**Isolation — never cross one client's connection with another's.** Every key gets its own MCP
session to every upstream (`ClientLink`), generic, nothing server-specific. The shared `Link` only
lists tools and checks health; it never runs a tool. A key's session opens on its first call, closes
after 15 idle minutes (CLIENT_IDLE), and closes at once when the key is revoked or deleted (or its
group is), the server is removed, or it goes down. Moving a key to another group keeps them, except on servers the new group doesn't reach. "Session terminated" from an upstream gets a
fresh session and one retry. Two chats of ONE key share sessions by design (one key = one app).
Cost: the browser upstream starts a worker container (max 8) per session that runs a tool.
Verified live 2026-10-10: two keys, two browsers; `end_session` on one left the other untouched.
The one exception is the admin's, per server: `upstreams.shared` (portal switch "Shared per group",
off by default) makes the session's owner the GROUP -- `("group", id)` instead of the key id
(`_owner`), so one person's devices share one memory / one browser. Flipping it closes that
server's sessions (`reconcile`); revoking a key leaves its group's shared session; deleting the
group closes it. `test_outage` covers both directions live (spiketools' per-session remember/recall).
Note: a server that keeps state globally (a memory server may) is shared by everyone
regardless -- the switch only matters for servers that key state by MCP session.

**Naming**: tool names pass through raw (clients like SillyTavern add their own prefix — pre-namespacing
would come back double-prefixed). Only collisions become `server__tool`, the server part cleaned by
`_prefix` (spaces etc. -> `_`: OpenAI-style clients, llama.cpp included, reject anything outside
[A-Za-z0-9_-] in a function name, and "Image Generator__x" would sink a whole list). Defaults are
names, so `_follow_defaults` (every rebuild) makes them follow the tool they named: a clash renames
them, its end renames them back, a renamed server's spelling follows (`_moved`), and a default that
was ambiguous from the start goes to the server the group's other defaults come from (`_guess`) or
stays flagged. `rename_upstream` also moves the server's events (stats, adaptive) and relearns.
Every such change goes to the `audit` table in a sentence -- one per clash, one per group -- and
the portal's Stats page shows it with what nobody uses. The first rebuild that knows any tools is
the baseline (a restart must not report standing clashes as new). Admin overrides (title,
description) change what the model reads and what search matches on.

**Adaptive** (`adaptive.py`, measured 2026-10-17 by replaying 1,584 real calls from six apps):
- Per KEY (one key = one app): score(B) = slow(B) + fast(B) + sum_A slow(A)*P(B|A). slow/fast are
  decayed counts in the key's own call count (half-lives 160 and 20 calls -- time passes only when
  the app works; LRFU/CRF, Lee et al. 1999); P is a forgetting transition table (IPAM, Davison &
  Hirsh 1998, alpha 0.8) -- the chain term pulls in the usual follower (get_job after generate).
- Hysteresis: in at 2.5 (one use scores 2.0, so a second use is needed -- "starts empty"), out
  below 1.0, a full list swaps its weakest only for a newcomer 20% better, into the same slot.
  The group's defaults never take a slot. CAP 10 by default (the measured one); a group may set
  `adaptive_cap` 1-50 -- a lowered cap drops the weakest first, and `gateway.recap()` re-picks every
  key at once when a cap or defaults change or a key moves group.
- Results (hit = tool already listed before the call): every call 0.677 (MRU-10 0.765 but twice
  the list churn; MFU-10 0.655); first use of a server per session 0.629 (n=35); list changes
  0.105/call. Chains added 11-14 points at that moment. Re-measure on new usage.
- The picks reach a client at its next tools/list; NO list_changed is sent for a reshuffle -- a
  changed tool list costs the client its prompt cache (llama.cpp reprocesses the whole prompt).
  `_picked[conv]` remembers what a conversation was given, so advice names the call it can make.
- Learning runs for every key in every mode (switching to adaptive starts warm). `learn()` replays
  the last 2000 events per key at startup -- the events log is the only memory. Events carry
  `key_id` (labels can be renamed or shared); old rows were matched by label in `_event_keys`.

## Search and embeddings

- Embeddings run ONLY inside the manager — no outside service, ever. Model: embeddinggemma-300m (gen 1), 4-bit ONNX
  (`onnx-community/embeddinggemma-300m-ONNX`, `onnx/model_q4.onnx`, ~220 MB), on onnxruntime +
  tokenizers; output `sentence_embedding`, 768-d. Downloaded on first start into `MCPM_MODELS_DIR`
  (the volume in Docker). Chosen 2026-10-13 over EmbeddingGemma 2 (measured on 107
  tools): mean rank of the right tool 1.38 vs 1.81, 12 ms vs 58 ms per query, ~600 MB vs ~910 MB RSS.
  Gen 1's int8 export is a trap: ~1.7 GB loaded. The embedder feeds any extra input an export
  declares as empty (gen 2 also wants image/video/audio), so either generation loads — but the
  floors below are per model.
- Never mix gen 1 and gen 2 vectors: same 768 dims, different space. (Vectors live only in memory,
  so a restart is the whole migration.)
- Prompts: query `task: search result | query: {text}`, document `title: {name} | text: {rest}`.
- A tool's document (`scoring.document`): name, admin title, the first ~300 chars of the description,
  `server: X - <server description>`. The server line is what makes "search the web" find SearXNG
  rather than the browser's generic `search`. Parameter names were measured to add noise.
- Vectors are cached by document TEXT, so any edit (override, server description, rename via the API)
  re-embeds just that tool, immediately (`gateway.reembed`). Embedding is off the request path; the
  background filler gives up after 8 failures and any search restarts it.
- Semantic only when every tool has a vector; otherwise the whole search is lexical (idf-weighted
  token overlap, best hit = 1.0, keep >= 0.5 of it). Never mix the two in one ranking. A word's idf
  has a floor of 0.01: at 0, a word in every tool found nothing, and with one server that is most
  words -- a new install's first server, until the model loads.
- Floors, measured on the live catalogue (107 tools, 16 labelled requests, 5 nothing can do), not
  chosen: cosine >= 0.30 AND within 0.12 of the best match. Unrelated tools score ~0.15 (p99 0.31),
  no-match requests top out at 0.28, the right tool never under 0.33; 0.12 is the smallest window that
  never cut the right answer (median 5-6 kept). Down-server pointing: best server >= 0.25 and >= 0.10
  ahead of the next. Each model has its own scale (gen 2: unrelated ~0.56, floor 0.6, window 0.05) —
  re-measure on any model or document-format change. A score of 0.5 is not "50% correct".

## Storage

SQLite, one file, WAL + synchronous=NORMAL. Tables: `groups(name, mode, defaults, adaptive_cap, servers)`,
`upstreams(name, url, enabled, timeout_ms, description, headers, tools)`, `keys(label, group_id, key_hash,
created_at, last_used_at, revoked)`, `tool_overrides(upstream, tool, title, description)`,
`events(ts, client_id, key_id, group_id, upstream, tool, latency_ms, status)` -- feeds /stats,
/stats/detail (the portal's Stats page) and the adaptive ranking. Keys are stored as SHA-256;
the secret exists only in the response that creates or re-issues it (re-issue keeps the row —
label, group — and revives a revoked key). `last_used_at` is written at most once a minute.
`config.yaml` seeds an EMPTY database only (no keys, no servers, on purpose: it ships in the repo
and the image); after that the DB is the truth.
Migrations in `db._migrate`: the `tools` column, and the old four-mode groups
(manual/reveal/semantic/hybrid) -> direct/hybrid, keeping every key.

## Running it

- Docker: `docker compose up -d --build`; container `mcp_manager-mcp-manager-1` on port 8899, on
  the host address `MCPM_PUBLISH` in `.env` says (127.0.0.1 if unset), volume `data` (DB + model). Model loads ~9 s after start; log line "search index: N tools, semantic".
- `.env` holds `MCPM_ADMIN_TOKEN`. Never print it: run API calls inside the container, e.g.
  `MSYS_NO_PATHCONV=1 docker compose exec -T mcp-manager python -c "...os.environ['MCPM_ADMIN_TOKEN']..."`
  (Git Bash on Windows mangles `/app`-style paths without MSYS_NO_PATHCONV=1).
- `.mcp.json` (gitignored — holds a live key) points Claude Code at the manager with the
  `claude-code` key (hybrid, no defaults), so Claude can test it as a real client through
  `mcp__mcp-manager__search_tools` / `call_tool`. In that role, answer only from tool results.
- Upstreams live in the DB (`config.yaml` ships none). Whoever runs them starts and stops them; don't
  restart them yourself.

## Tests

`python -m pytest -q` from the repo root — 198 tests.
- Fast, no network: `test_names`, `test_adaptive`, `test_group_servers`, `test_tls`, `test_supervisor`, `test_native`, `test_advice`, `test_conversations`, `test_hints`, `test_reembed`,
  `test_migration`, `test_embedder`, `test_config`. Run these freely.
- Live: `test_api`, `test_gateway`, `test_search`, `test_coaching`, `test_outage` start their own
  manager on :8898 (`tests/config_test.yaml`, fresh `tests/test.db`, lexical search, seeded keys
  `mgrtest_alpha` = direct, `mgrtest_beta` = hybrid). `test_api`, `test_gateway`, `test_search` and
  `test_coaching` need imagegen, browser and ytdl reachable, at the addresses in
  `tests/upstreams.local.yaml` (gitignored, so private addresses stay out of the repo; conftest adds
  them to the config). Without that file those four (and `test_a_blocked_fetch...` in `test_outage`)
  skip, so a fresh clone's suite passes. `test_outage` borrows ports 8896 and 8897.
  To run a test manager by hand, `tests/config_test.yaml` alone now has no servers.
  `test_outage` also runs `spiketools.py` over HTTP on :8896 and kills/restarts it.
- **Browser worker slots**: every MCP session that runs a browser tool spawns a worker (cap 8,
  reaped after 30 min idle). Repeated live runs exhaust it and tests then look flaky. Wait for
  reaping; never kill running workers.

## Engineering rules

Simple and efficient. If a design needs a paragraph to justify it, it's too complicated — say so first.
1. No speculative abstraction: the function, not the framework.
2. No config for a thing that could be a decision; hardcode it and say why in a comment.
3. No dependency for what ten lines do, unless it is protocol work (MCP SDK, sqlite, the embedder).
4. The hot path stays dumb; anything expensive (embedding, stats) runs off the request path.
5. Fail loud and specific: name the upstream, name the method. A generic error is a bug.
6. One container, one process.
7. Every feature lands with a test in `tests/`; prefer a deterministic one where possible.
8. Comments explain why, never what.
Don't commit without asking. Keep secrets out of committed files and out of chat.

## Research conclusions (2026-10)

- **`list_changed` cannot be relied on.** Of the frontends served, only pi refreshes on it. LM Studio
  has a manual refresh button, Open WebUI caches tools to its DB, SillyTavern's MCP extensions cache
  by hand, and the llama.cpp webui never sends `Mcp-Session-Id` so it cannot receive server messages.
  Claude Code, VS Code, Windsurf, Cline, Kilo Code, Opencode and Glama honour it; Cursor does not.
  Hence `call_tool` as the primary path.
- Fewer visible tools select better: RAG-MCP (arXiv:2505.03275) 43.13% vs 13.62% accuracy, >50%
  fewer prompt tokens. Working set 20–30 tools; in hybrid the admin picks the defaults.
- MCP spec 2026-07-28 went stateless (no handshake/session; identity in `_meta`), but real frontends
  still speak the handshake era. Identity here comes from the credential, never the transport.
- Clients treat MCP as tool discovery; resources and prompts are mostly ignored.
- **The manager should not hand out certificates to remote users.** MCP (streamable HTTP) requires no
  TLS and advises localhost for local servers; a key that never leaves the machine needs no TLS. Remote
  HTTPS is delegated to Tailscale Serve/Funnel, Cloudflare Tunnel or Caddy — real certs, zero trust
  ceremony on Mac/Win/Linux. Decided 2026-10-17 after a user's client choked on the self-signed cert.
- **Localhost until told otherwise, in every install** (2026-11-06: the manager's job is security).
  Bare installs bind 127.0.0.1; compose sets `MCPM_HOST=0.0.0.0` (a port mapping only reaches that
  bind) and `MCPM_PUBLISH`, default 127.0.0.1, picks the HOST address. `tests/test_tls.py` pins both.
- **The installers' certificate** (2026-11-06) is the opt-in for a LAN with no proxy. `make-tls.sh`
  needed bash and openssl, which stock Windows lacks, and its output was a lone CA:TRUE certificate:
  trusted on a device, a stolen key could forge any site there. `tls.py` makes a root and a server
  certificate instead, and never stores the root's private key, so nothing on disk can sign a second
  certificate; the price is that new names mean trusting a new root. It runs inside the image and
  writes to the `data` volume, because a key on a bind mount may be unreadable to the container's
  non-root user on Linux. 825 days counts from notBefore: it starts a day early, so ends 824 days out.
  Server certs need EKU serverAuth on Apple platforms. Node ignores the OS store, hence
  NODE_EXTRA_CA_CERTS: that is what broke Claude Code in 2026-10. The trust step's Linux path was
  tested on Debian in a container, and Windows only up to the store; macOS not at all.

## Not done / open

- No OAuth (API keys), no stdio upstreams, no resources/prompts routing, no per-user identity inside
  one frontend.
- The browser's own `list_sessions` shows every session's id/idle time to any key (metadata, not
  content) — the manager could withhold that tool from app keys if wanted.
- Pruning the `events` log is not implemented.
