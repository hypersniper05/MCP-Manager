# MCP-Manager — guide for LLMs

This document is written for a language model (or an agent harness) that has been asked to
**install** MCP-Manager, **connect** an app to it, **use** its tools, or **build an admin page**
for it in a frontend or harness. It is self-contained: everything needed is here, with exact
request and response shapes. Where this guide and the code disagree, the code wins — the API is
implemented in `mcp_manager/api.py`, and `portal/index.html` is a complete working admin page.

---

## 1. What it is, in one minute

MCP-Manager is **one MCP server in front of many**. You register your MCP servers ("upstreams")
with it once. Every app that wants tools connects to the manager instead, with its own **key**.
Each key belongs to a **group**, and the group decides what that app's model sees.

```
apps (each with its own key) ──► MCP-Manager ──► upstream MCP servers
                                 key → group → tool view
```

| Term | Meaning |
|---|---|
| **Upstream** (server) | A real MCP server registered with the manager. Streamable HTTP only. |
| **Key** | An app's credential (`Authorization: Bearer <key>`). Belongs to exactly one group. Stored only as a SHA-256 hash. |
| **Group** | A tool view. Mode `direct`, `hybrid` or `adaptive`. |
| `direct` mode | Every tool from every connected server is in the app's tool list. |
| `hybrid` mode | The group's `defaults` (a few chosen tools) plus four manager tools: `search_tools` and `call_tool`, which reach every other tool, and `list_mcpservers` and `get_tools_from_server`, which show what each server has. |
| `adaptive` mode | `hybrid`, plus the tools (10 by default, up to 50 per group: `adaptive_cap`) each key's app is likely to use, learned from that key's own calls (frequency and recency at two speeds, plus which tool usually follows which). A new key starts with none; a tool needs a second use to be listed and leaves when it falls out of use. Changes reach the app the next time it lists its tools; no `list_changed` is sent just for them. |
| **Exposed name** | The name an app calls a tool by. The server's own name, unless two servers share a name — then both become `server__tool`, with spaces and other characters clients reject in the server name turned into `_` (`Image_Generator__server_status`). Group defaults follow these renames automatically. |
| **Override** | An admin-written title/description for a tool, replacing the server's text in what the model reads and in what search matches. |
| **Admin token** | The secret for the management API and admin page. Different from app keys. Never give it to a model or an MCP client. |

Two ports of entry, one process, one port (default **8899**):

| Path | Who | Auth |
|---|---|---|
| `/mcp` | MCP clients (apps, models) | an app key |
| `/api/v1/*` | admin pages, harnesses, scripts | the admin token |
| `/` | the bundled admin page (static HTML) | none to load; it asks for the admin token |

An MCP client can never reach the management API: a key is rejected on `/api/*`.

---

## 2. Install

### With the installer (recommended)

```bash
git clone https://github.com/hypersniper05/MCP-Manager.git && cd MCP-Manager
./install.sh --yes       # Windows: install.bat --yes
```

`install.sh` is not tested on macOS yet. If it fails there, use the Docker steps below.

It needs Docker. It writes a random admin token to `.env`, builds and starts the container, and
prints the addresses. Safe to run again: it keeps the token, the data and a certificate that still
fits. Without `--yes` it asks two questions; these answer them in advance:

| Option | What it does |
|---|---|
| `--network` | Other devices on the network can reach it (the default is this computer only) |
| `--https` / `--http` | Turn HTTPS on, or back off |
| `--name <host>` | A host name the certificate must cover (repeat for more) |
| `--port <n>` | Another port (default 8899) |
| `--no-trust` | Do not add the certificate to this computer's trust store |
| `--trust <file>` | Only trust a certificate, on another device that connects |

### With Docker, by hand

```bash
git clone <this repository> && cd <it>
cp .env.example .env
# put a long random secret in .env as MCPM_ADMIN_TOKEN, e.g.:
python -c "import secrets; print('mcpm_admin_' + secrets.token_urlsafe(24))"
docker compose up -d --build
```

- Published on `127.0.0.1:8899`: this computer only. `MCPM_PUBLISH=0.0.0.0` in `.env` opens it to
  the network, and `MCPM_PORT` moves it off 8899. The SQLite database and the embedding model live
  in the `data` volume.
- On first start it downloads the embedding model (~220 MB, from Hugging Face, not gated). Until it
  has loaded, search works by word matching; nothing else waits for it. When it is ready the log
  says `search index: N tools, semantic`.
- Check it is up: `curl -H "Authorization: Bearer $MCPM_ADMIN_TOKEN" http://localhost:8899/api/v1/upstreams`

### Without Docker

Python 3.11+.

```bash
pip install -r requirements.txt
python -m mcp_manager config.yaml
```

The `.env` file next to `config.yaml` is read automatically. The model downloads into the
`embeddings.dir` directory (default `models/embeddinggemma-300m`, relative to the working directory).
It listens on `127.0.0.1` only unless `boot.host`/`MCPM_HOST` says otherwise.

### HTTPS

On localhost none is needed. For other devices, in order of preference:

1. **A TLS-terminating proxy.** `tailscale serve --bg http://127.0.0.1:8899` gives a real certificate
   with nothing to trust on any client, and the manager stays plain HTTP on localhost. Cloudflare
   Tunnel and Caddy work the same way.
2. **The manager's own certificate.** `./install.sh --network --https` (Windows:
   `install.bat --network --https`) makes one and serves it. Without Docker:
   `python -m mcp_manager.tls --dir certs <host> <address>`, then point `MCPM_TLS_CERT` and
   `MCPM_TLS_KEY` at `certs/tls-cert.pem` and `certs/tls-key.pem`.

The second way makes a root certificate and the server certificate it signs, and never stores the
root's private key, so the root cannot sign anything else. Clients must trust the root
(`certs/mcp-manager-root.pem` from the installer, `certs/ca.pem` from the command). The installer
offers to add it to this computer's trust store and sets `NODE_EXTRA_CA_CERTS` for the user; other
devices run `--trust <file>`. Clients with their own list need telling: Python apps through
`SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` for that app alone, containers by mounting the file. Both certificates last 825
days, and new names mean a new root.

With HTTPS on, plain `http://` stops answering, and every address becomes `https://`.

### Environment variables

| Variable | Purpose |
|---|---|
| `MCPM_ADMIN_TOKEN` | Admin token for `/api/v1/*`. **Unset → the API answers 503** (fails closed, never open). |
| `MCPM_DB` | Path of the SQLite database (overrides `boot.db`). |
| `MCPM_MODELS_DIR` | Where the embedding model is stored (overrides `boot.embeddings.dir`). |
| `MCPM_HOST` | Interface to bind (overrides `boot.host`). Default `127.0.0.1` — local only. Docker compose sets `0.0.0.0` (a port mapping only reaches that bind). |
| `MCPM_PUBLISH` | Docker only: the host address the port is published on. Default `127.0.0.1` — this computer only. `0.0.0.0` opens it to the network. |
| `MCPM_PORT` | Docker only: the port on the host. Default `8899`. |
| `MCPM_TLS_CERT`, `MCPM_TLS_KEY` | Certificate and key: both set → the manager serves HTTPS itself (uvicorn, same process, no proxy). Half a pair, or a missing file → the start fails and names what is missing. Neither → plain HTTP. |
| `HF_TOKEN` | Only if your network needs a token to download from Hugging Face. |

### `config.yaml`

Two parts: **boot config** (read on every start) and a **seed** (applied only when the database is
empty). After the first start the database is the truth; edit through the API or admin page.
Editing the seed later does nothing unless you delete the database.

```yaml
boot:
  host: 0.0.0.0            # default 127.0.0.1
  port: 8899
  db: mcp_manager.db
  default_group: default   # group used for keys created without one (and for keyless access)
  require_key_for_mcp: true   # false = /mcp accepts anyone, with the default group's view
  allowed_hosts: null      # set e.g. ["myhost:*"] to turn on DNS-rebinding protection
  tls_cert: null           # with tls_key: serve HTTPS directly (python -m mcp_manager.tls makes a pair)
  tls_key: null            # both files or neither — half a pair stops the start
  portal_file: portal/index.html
  embeddings:              # the in-process model; usually leave as is
    dir: models/embeddinggemma-300m
    repo: onnx-community/embeddinggemma-300m-ONNX
    file: onnx/model_q4.onnx           # set file: null to disable embeddings (word matching only)
    threads: 4

upstreams:                 # seed: servers
  - name: browser
    url: http://browser-host:8000/mcp
    description: Drives a real browser.    # optional: what the model is told the server is for
    headers: {Authorization: "Bearer ..."} # optional: sent with every request, for servers behind auth
    enabled: true                          # optional

groups:                    # seed: groups
  - name: default
    mode: hybrid
  - name: everything
    mode: direct
  - name: research
    mode: hybrid
    defaults: [search, read_text]          # exposed tool names

keys:                      # seed: keys -- only for tests; never commit a real key
  - key: some-secret
    label: test app
    group: research
```

The admin token can also be set as `boot.admin_token`, but the environment variable wins and is
the right place for it.

**Networking note:** an upstream URL is resolved from where the manager runs. Inside Docker,
`localhost` is the container itself — use the host's address or `host.docker.internal` for a
server running on the host.

### First-time setup, in order

1. Add servers: `POST /api/v1/upstreams` (or the admin page's Servers tab).
2. Create or adjust groups: `POST /api/v1/groups`.
3. Create one key per app: `POST /api/v1/keys` — copy the `key` from the response; it is shown once.
4. Configure the app with the MCP endpoint and that key (next section).

---

## 3. Connect an app

- Endpoint: `http://<host>:8899/mcp`, transport **streamable HTTP**.
- Header: `Authorization: Bearer <app key>`.
- The client must support HTTP MCP servers with a custom header. Stdio-only clients need an
  HTTP-to-stdio bridge.

A typical `mcp.json` / `.mcp.json`:

```json
{
  "mcpServers": {
    "mcp-manager": {
      "type": "http",
      "url": "http://<host>:8899/mcp",
      "headers": {"Authorization": "Bearer <app key>"}
    }
  }
}
```

Errors on connect: `401 {"error": "a registered key is required"}` — the key is unknown, revoked or
deleted.

**One key = one app.** Each key gets its own session on every upstream server, so apps never share
server-side state (browser tabs, logins, jobs). Two chats of the same app (same key) do share it.
Give separate apps separate keys. The exception is the admin's, per server: a server marked
`shared` gives all keys in one group a single session there — one person's devices sharing one
memory or one browser — while other groups stay apart.

### Receiving tool-list changes (for client developers)

The manager's tool list for a session changes: in hybrid mode the first use of a server adds all
its tools, and servers going down, coming back, being added or removed change it too. Each time,
the manager sends `notifications/tools/list_changed`. A client that wants the new tools as real
entries (instead of reaching them through `call_tool`) does this:

1. `initialize` with a handshake-era `protocolVersion` (`2025-11-25` or `2025-06-18`). Keep the
   `Mcp-Session-Id` response header and send it, with `MCP-Protocol-Version: <negotiated>`, on every
   later request. Then send `notifications/initialized`. The result advertises
   `capabilities.tools.listChanged: true`.
2. Open the listen stream: `GET /mcp` with `Accept: text/event-stream` and the same headers, and keep
   it open. **The notification arrives on this stream only** — not on the response stream of the tool
   call that caused it — and is not replayed if no stream is open.
3. On `notifications/tools/list_changed`: re-fetch `tools/list` (following `nextCursor`), replace the
   cached list, and rebuild the tools given to the model — before the model's next step, even within
   the same turn (the change usually happens during a tool call). Debounce bursts.
4. If the stream drops, reopen it and re-fetch `tools/list` once. A `404` on the session means it is
   gone (e.g. the manager restarted): initialize again. `DELETE /mcp` with the session ID when done.
   An idle stream carries an SSE comment (`: ping - <time>`) every 15 s; ignore it. A session with
   no open stream and no request running is closed after 30 minutes, so a client that never sends
   `DELETE` still gets cleaned up — but only once its stream is closed.
5. No stream possible? Re-fetch `tools/list` before each model step instead; it is cheap.

The manager does not serve the stateless `subscriptions/listen` (protocol 2026-07-28) yet. A session
is one conversation: servers opened in it stay in its list for an hour after their last use.

---

## 4. Using the tools (for the model behind an app)

The manager's MCP instructions list every server: name, tool count, state, and description. In
hybrid mode the same list is in `search_tools`' description (each description shortened to its
opening), so it is visible even in clients that hide server instructions. Both are as old as your
client's last tool-list fetch; `list_mcpservers` is always current.

### Direct mode

Every tool is in your list under its exposed name. Call it normally.

### Hybrid mode

Your list holds the group's default tools plus:

```
search_tools          {query: string, server?: string, page?: integer (default 1), page_size?: integer 1-20 (default 5)}
call_tool             {name: string, arguments?: object}
list_mcpservers       {}
get_tools_from_server {server: string}
```

- `list_mcpservers` → `{"<server>": {"description": "...", "tools": 16, "status": "up" | "down" | "connecting"}}`,
  live. The description is the admin's when one is set, the server's own otherwise.
- `get_tools_from_server` → `{"server": "...", "status": "...", "tools": {"<tool>": "what it does (args: a, b?)"}}`:
  an overview of one server, to choose from. Argument names only; a search shows them in full.
  A server's name can be given loosely (`"browser"` for `secure_browser`).
- `search_tools` with `server` searches that server only, and ranks within it.

**The flow:**
1. If you know a tool's name (from your list, a search, or an earlier result), call it. Tools in
   your list are called directly; any other tool runs through `call_tool`.
2. If you don't, call `search_tools` with a few words describing **one step** ("take a screenshot",
   "download a youtube video"). Each match shows its server and its arguments, ready to use. To see
   everything one server offers instead, call `get_tools_from_server`.
3. Run the match: `call_tool {"name": "<exposed name>", "arguments": {...}}`.

```
→ search_tools {"query": "take a screenshot of the page"}
← 2 matches for "take a screenshot of the page":
  screenshot [browser] Screenshot the viewport, an element, or the full page.
    args: target?: string; fullPage?: boolean
  snapshot [browser] List the interactive elements in view as refs (r1, r2…).
    args: all?: boolean

  Run one with call_tool, e.g. call_tool {"name": "screenshot", "arguments": {}}

→ call_tool {"name": "screenshot", "arguments": {"fullPage": true}}
← <the image>
  [mcp-manager] You just used browser. All 20 of its other tools are open to you now;
  call any of them by name (through call_tool if it is not in your tool list), no search needed:
  - navigate(url) — ...
```

In argument signatures, `?` marks an optional argument, `= x` a default, `a–b` a range. A match
shows the opening of its description, what each required argument takes, and the type of each
optional one; a call that gets an argument wrong is answered with the full usage. Matches are in
order of fit (no scores). A server that is down is said once, at the top, and its matches carry
`down`.

**Read the `[mcp-manager]` notes** at the end of results. They save you calls:

| You did | You get |
|---|---|
| First use of a server | Every other tool on it (in full for small servers; the closest ten plus the rest by name for big ones). No need to search that server again. The tools also join your tool list if your client re-fetches it. If you already listed it with `get_tools_from_server`, just a one-line reminder. |
| A name spelt differently (`listTabs`, `browser.ListTabs`) | It runs; the note gives the exact name. |
| A misspelt name | "Did you mean X?" with X's arguments. (It runs straight away only for read-only tools.) |
| A name like no tool | "No tool has a name like it", then the nearest tools by purpose — check one fits before calling it; none may. |
| An argument slip with one reading (wrong case, `"10"` for a number, `null` for an optional, a near-miss key) | It is fixed and runs; the note says what to send next time. |
| A missing required argument | Refused before reaching the server, with the full usage and every other correction. Resend the whole corrected call. |
| `call_tool` with `tool`/`args`, or arguments next to `name` instead of inside `arguments` | Read the way you meant; the note shows the right shape. |
| A tool that fails at its job (blocked, 403/404, empty page) | The names of tools on other servers that can do the same job. |
| A server that is down | "X is down right now; the manager keeps trying to reconnect." Its tools stay listed. Don't loop on it; try again later or use an alternative. |
| A server that restarted (before or during your call) | Within seconds: a call it never ran, or a read-only one it cut off, is run again on a fresh session; any other call cut off is reported as "may or may not have taken effect — check before running it again". Either way you are told that what the server kept for you (open pages, logins, jobs) is gone. |
| Anything, after a server changed state | A one-time heads-up: a server went down, came back, was added or removed. |

Results that a server returns as structured data arrive through `call_tool` as JSON text, followed
by any notes.

A server stays "open" for a conversation for an hour after its last use; after that, its first use
lists its tools again. A conversation is the client's MCP session if it sends `Mcp-Session-Id`,
otherwise the key.

---

## 5. Build an admin page (frontend UI or harness)

The management API is the whole admin surface: everything the bundled page at `/` does, your page
can do with the same calls. Read `portal/index.html` for a complete, dependency-free example.

### Rules that keep it safe

- The **admin token** grants full control. In a browser UI, let the admin paste it at runtime (keep
  it in memory or the browser's storage) — never bake it into a public bundle. In a server-side
  harness, keep it in the server's environment and proxy the calls.
- Never put the admin token, or an app key, in front of a model.
- App keys are shown **once**, in the response that creates or re-issues them. Show the key with a
  copy button and a warning; it cannot be fetched again (only re-issued, revoked or deleted).
- CORS is open (`*`), so a page on another origin can call the API directly. The token is the boundary.

### Basics

- Base URL: `http://<host>:8899/api/v1`
- Every call: `Authorization: Bearer <admin token>`. JSON in and out (`content-type: application/json`).
- Errors are `{"error": "message"}` with a real status:

| Status | Meaning |
|---|---|
| 400 | Bad input (missing name, bad URL, bad mode, body not a JSON object) |
| 401 | Missing or wrong admin token (an app key is not an admin token) |
| 404 | No such group / server / key |
| 409 | Name already taken (`"conflict: ..."`) |
| 503 | No admin token configured on the manager |

- **Slow calls, by design:** `POST`/`PATCH /upstreams` wait up to ~20 s for the connection attempt
  (so the answer says whether it worked), and `PATCH /upstreams` and `POST /overrides` wait up to
  ~60 s while changed tools are re-embedded (so the next search reflects the edit). Use a request
  timeout of at least 90 s for these and show progress; use short timeouts elsewhere.
- Nothing is pushed: poll `GET /upstreams` (every 5–10 s) for live server health.

### Endpoint reference

#### Groups

```http
GET    /groups
POST   /groups            {"name": "pi", "mode": "hybrid", "defaults": ["navigate", "screenshot"]}
PATCH  /groups/{name}     any of {"name", "mode", "defaults", "adaptive_cap", "servers"}
DELETE /groups/{name}
```

Group object (returned by GET as a list, by POST with `201`, by PATCH):

```json
{"id": 4, "name": "pi", "mode": "hybrid", "defaults": ["navigate", "screenshot"],
 "adaptive_cap": 10, "servers": null,
 "unresolved": [], "sees": 2, "keys": ["pi on the laptop"]}
```

- `mode`: `"direct"`, `"hybrid"` or `"adaptive"` (default `hybrid`). `adaptive_cap`: how many
  learned tools an adaptive group lists, a whole number 1–50, `null` for the default (10); it comes
  back as the value in effect. Changing it or the defaults re-picks the group's apps' lists at once. `defaults`: exposed tool names (from
  `GET /tools`, field `exposed`). They are kept when switching to `direct`, so switching back
  restores them.
- `servers`: which servers the group reaches. `null` (the default) is all of them, including any
  added later. A list of names limits it to those; `[]` reaches nothing, and a name that is not
  registered is a `400`. Tools on any other server cannot be listed, searched or called with the
  group's keys, in any mode and through any spelling: to those keys the server does not exist. Taking
  a server out closes the sessions the group's keys held there. Renaming a server keeps it chosen;
  deleting one takes it out of every list. Show it as a checkbox, "Automatically add all MCP
  servers", which when turned off offers the servers to tick, starting from all of them ticked.
- `unresolved`: defaults that lead nowhere right now (server down or not in the group, or the tool
  renamed upstream) — show these as warnings. They are kept, so putting the server back restores them.
- `sees`: how many upstream tools are in the group's list right now (not counting the manager's
  own tools).
- `keys`: labels of the group's active keys.
- `PATCH` with `name` renames the group (`409` if taken).
- `DELETE` **also deletes the group's keys** and disconnects them:
  `{"ok": true, "deleted_keys": 2}`. Confirm with the admin first.

#### Servers (upstreams)

```http
GET    /upstreams
POST   /upstreams         {"name": "browser", "url": "http://host:8000/mcp",
                           "description": "...", "headers": {"Authorization": "Bearer ..."},
                           "enabled": true, "shared": false, "timeout_ms": 30000}
PATCH  /upstreams/{name}  any of {"name", "url", "description", "headers", "enabled", "shared", "timeout_ms"}
POST   /upstreams/{name}/reconnect  -> the server object, after the first attempt
DELETE /upstreams/{name}  -> {"ok": true}
POST   /test-connection   {"url": "http://host:8000/mcp", "headers": {...}}
```

Server object (GET returns a list; POST returns one with `201`; PATCH returns one):

```json
{"name": "browser", "url": "http://host:8000/mcp", "enabled": true,
 "status": "connected", "connected": true, "error": null,
 "down_for": null, "retry_in": null,
 "headers": {"Authorization": "Bearer ..."}, "shared": false,
 "tools": 21, "sessions": 2,
 "description": "Drives a real browser ...", "overridden": true,
 "server_says": "the server's own instructions, shortened"}
```

- `status`: `connected` · `connecting` · `down` · `disabled`. While `down`: `error` says why,
  `down_for` is seconds since it went down, `retry_in` is seconds to the next reconnect attempt
  (backoff 2 s doubling to 60 s; also retried at once whenever a client needs it).
- `tools`: tool count (a down server keeps its last known tools). `sessions`: sessions open on it
  (one per app, or one per group when `shared`).
- `POST .../reconnect` drops the manager's session to the server and opens a new one (its headers
  sent again -- use it after changing a credential on the server's side), lists its tools again, and
  closes every app's own session there; those reopen on their next call, without what the server
  kept for them. `400` if the server is turned off, `404` if there is none. Ask before it when
  `sessions` > 0.
- `shared` (default `false`): every key in a group uses ONE session on this server, so they see the
  same state there. Changing it closes the server's open sessions; each next call opens one under
  the new rule. Revoking one key leaves its group's shared session to the others. Show it as a
  switch that asks before turning on: it is the one exception to per-app isolation.
- `description`: what the model is told the server is for. `overridden: true` means the admin wrote
  it; otherwise it comes from the server (`server_says`). Send `""` or `null` to go back to the
  server's own words.
- `url` must start with `http://` or `https://` and be a streamable-HTTP MCP endpoint (usually
  ending in `/mcp`). Changing it forgets the cached tool list.
- `timeout_ms` (default 30000) limits connecting, listing and health checks. Tool calls have their
  own 10-minute limit.
- `headers` (optional): extra HTTP headers sent with every request to this server — for servers
  behind auth. Stored as given (they must be sent as-is, so unlike keys they are not hashed) and
  listed back, so an editor can pre-fill them; the admin token is the boundary. Send `{}` or
  `null` to clear. Changing them reconnects the server. A wrong token reads as a refused
  connection (401), not a separate error.
- `POST /test-connection` tries an endpoint without saving it — for an "Add server"
  form's Test button. Body `{"url", "headers"?}`, answer `{"ok": true, "tools": 12, "server": "..."}` (the server's
  own title, may be null) or `{"ok": false, "error": "..."}`. It answers `200` in both cases:
  the question was "can you reach it", and yes/no is the answer. Nothing is stored, no server is
  registered, no background link starts. Times out after 15 s with `ok: false`.
- Renaming keeps the server's tool overrides. `enabled: false` disconnects it and hides its tools.

#### Tools and overrides

```http
GET  /tools
GET  /overrides
POST /overrides       {"upstream": "browser", "tool": "list_frames",
                       "title": "...", "description": "..."}   -> {"ok": true, "reindex": true}
```

`GET /tools` — every tool, by the name a client calls:

```json
[{"exposed": "list_frames", "upstream": "browser", "tool": "list_frames",
  "description": "the text the model reads", "overridden": false,
  "server_says": "the server's own description",
  "arguments": "tab?: string (which tab)"}]
```

- `description` is the text in full. `server_says` is the server's own, the same text unless
  `overridden`: what a Revert button puts back. Removing the override reverts it.
- `arguments` is a one-line human/model-readable signature, not a JSON schema.
- `exposed` differs from `tool` only on a name collision (`browser__list_downloads`).

`GET /overrides` — `[{"upstream", "tool", "title", "description", "updated_at"}]`.

`POST /overrides` sets an override by the server's own tool name (`upstream` + `tool`, not
`exposed`). Blank or `null` for both `title` and `description` removes it. It changes what the model
reads and what search matches, immediately.

#### Keys

```http
GET    /keys
POST   /keys              {"group": "pi", "label": "pi on the laptop"}
PATCH  /keys/{id}         any of {"group", "label"}     -> {"ok": true}
POST   /keys/{id}/revoke                                -> {"ok": true}
POST   /keys/{id}/reissue                               -> {"id", "key", "label", "group"}
DELETE /keys/{id}                                       -> {"ok": true}
```

- `POST /keys` → `201 {"id": 7, "key": "mcpm_...", "label": "pi on the laptop", "group": "pi"}`.
  **`key` is the only copy of the secret.** `group` defaults to the default group; `label`
  defaults to `key-<hint>`. Unknown group → `404`.
- `GET /keys` → `[{"id", "label", "group_name", "created_at", "last_used_at", "revoked", "hint"}]`.
  Note the field is `group_name`. `hint` is the first 8 hex characters of the key's hash, for
  telling keys apart. `last_used_at` updates at most once a minute (ISO 8601, UTC).
- `PATCH` moves a key to another group (the app keeps working, with the new view) or renames it.
  It returns `{"ok": true}`, not the key — re-fetch `GET /keys`.
- Revoke and delete both cut the app off at once and close its sessions on every server. A revoked
  key stays listed (`revoked: true`).
- `POST /keys/{id}/reissue` → `{"id", "key", "label", "group"}` — a new secret for the same row:
  same label, same group, old secret dead at once, `revoked` cleared (a re-issued key is live).
  This is how a lost secret is recovered without deleting and re-creating the row.

#### Search and usage

```http
POST /search    {"query": "take a screenshot", "page": 1, "page_size": 10}
GET  /stats
GET  /stats/detail?days=30
GET  /keys/{id}/adaptive
GET  /audit
```

`GET /audit` → what the manager changed by itself, written as sentences for a person:

```json
{"renamed": [{"tool": "server_status", "servers": [
   {"upstream": "Image Generator", "name": "Image_Generator__server_status"},
   {"upstream": "Claude Chat", "name": "Claude_Chat__server_status"}]}],
 "events":  [{"ts": "...", "kind": "default",
              "message": "Group \"imagelibrary\": its default server_status is now called Image_Generator__server_status, ..."}]}
```

`renamed` is the state now; `events` the history, newest first (200 at most). `kind`: `rename` (a
clash began or ended — one event per clash, not per tool), `default` (a group's default followed a
rename) or `edit` (a server or group renamed in the settings: its history and defaults moved with it).

`POST /search` runs the same engine as the model's `search_tools`:

```json
{"mode": "semantic", "query": "take a screenshot", "total": 3, "page": 1,
 "page_size": 10, "pages": 1,
 "results": [{"exposed": "screenshot", "mcp": "browser", "tool": "screenshot",
              "description": "...", "score": 0.81, "source": "mcp server",
              "available": true, "arguments": "target?: string; fullPage?: boolean"}]}
```

- `mode`: `semantic` (the embedding model) or `lexical` (word matching, while the model loads or
  if it is disabled). `score` is 0–1 in both; rank matters more than the number. Semantic search
  returns matches scoring at least 0.3 and within 0.12 of the best one; lexical returns matches with
  at least half the best match's score. An empty `query` returns every tool, unranked.
- `source`: `"edited here"` when an override supplied the description. `available`: its server is
  connected.

`GET /stats` → two usage series for the overview:

```json
{"tools": [{"upstream": "browser", "tool": "screenshot", "calls": 42, "last_seen": "..."}],
 "days":  [{"day": "2026-10-16", "calls": 42, "failed": 1}]}
```

`tools` is the 50 most-called; `days` is per-day call counts (and how many did not succeed) for the
last 90 days, oldest first. Days with no calls are absent — a volume chart should fill its own gaps.

`GET /stats/detail?days=N` (N 1–365, default 30) → everything a statistics page needs for that
window. It reads every event in the window, so fetch it when the page is opened, not on a poll:

```json
{"days": 30, "calls": 1287, "status": {"ok": 1197, "error": 84, "invalid": 5, "down": 1},
 "p50_ms": 300, "p95_ms": 9000,
 "tools":   [{"upstream": "browser", "tool": "screenshot", "name": "screenshot", "calls": 360,
              "failed": 21, "p50_ms": 300, "p95_ms": 9000, "last_seen": "..."}],
 "servers": [{"upstream": "browser", "calls": 726, "failed": 44, "p50_ms": 300, "p95_ms": 9000, "last_seen": "..."}],
 "apps":    [{"key_id": 2, "label": "pi", "group": "default", "mode": "adaptive", "revoked": false,
              "calls": 748, "failed": 52, "p50_ms": 300, "p95_ms": 9000, "last_seen": "...", "tools_used": 14,
              "top": [{"upstream": "browser", "tool": "screenshot", "calls": 180}],
              "adaptive": [{"upstream": "browser", "tool": "screenshot", "name": "screenshot",
                            "score": 41.2, "picked": true}]}],
 "heat":    [[0, 0, 3, "... 24 numbers: Monday, hour 0-23 UTC"], "... 7 rows, Monday first"],
 "chains":  [{"from": {"upstream": "browser", "tool": "navigate"},
              "to": {"upstream": "browser", "tool": "screenshot"}, "count": 180}]}
```

- `status`: `ok`, `error` (the tool ran and failed), `invalid` (refused before the server: the
  arguments were wrong), `down` (its server was unreachable). `failed` counts everything but `ok`.
- Latency is in milliseconds, nearest-rank percentiles over the window, failures included.
- `heat` is in UTC; shift it by the viewer's offset before drawing an hour-of-day grid.
- `chains`: the same app calling one tool and then a different one within 30 minutes, top 15.
- `apps[].adaptive`: that key's ranking, best first — `picked` are the (up to 10) tools it lists
  or would list; the rest are next in line. A key whose events predate it shows by label.

`GET /keys/{id}/adaptive` → one key's ranking: `{"id", "label", "group", "mode", "listed",
"tools": [{"upstream", "tool", "name", "score", "picked"}]}`. Learned for every key; `listed` is
true when its group is adaptive, i.e. when the picks are really in its app's tool list. Scores
are relative (a tool used once scores about 2; it is listed from 2.5), not probabilities.

### Screens to build

| Screen | Calls | What to show and do |
|---|---|---|
| **Overview** | `GET /upstreams`, `GET /stats`, `GET /keys` | Servers up/down (with `error`, `retry_in`), total tools, active keys, a call-volume chart from `stats.days`, most-used tools from `stats.tools`. Poll servers every 5–10 s. |
| **Servers** | `GET/POST/PATCH/DELETE /upstreams`, `POST /test-connection` | List with status badges. Add form (name, URL, optional auth headers as `Name=Value` lines with an `Authorization=Bearer <token>` example, optional description, timeout) with a **Test connection** button that enables once the URL field holds an `http(s)://` value and shows the tool count or the error — without saving. Edit description inline; blank = server's own. Enable/disable toggle. Confirm before delete. Expect add/edit to take a few seconds. |
| **Groups** | `GET/POST/PATCH/DELETE /groups`, `GET /tools` | Mode toggle (direct/hybrid/adaptive). "Automatically add all MCP servers" checkbox (`servers: null`); turned off, one checkbox per server from `GET /upstreams`. Defaults picker fed by `GET /tools` (`exposed` names, only the group's servers), relevant in hybrid and adaptive. Show `unresolved` as warnings and `sees` as the list size. Warn that deleting a group deletes its keys. |
| **Keys** | `GET/POST/PATCH /keys`, `POST /keys/{id}/revoke`, `POST /keys/{id}/reissue`, `DELETE /keys/{id}` | Create (group + label) → show the key once with a copy button and the connection snippet from section 3. Move between groups, rename, re-issue (new secret, shown once like a new key — confirm first that the old one dies), revoke, delete. Show `last_used_at`. |
| **Tools** | `GET /tools`, `GET /overrides`, `POST /overrides` | Searchable, scrollable table (there can be hundreds). A way to read a description in full, since a table row cuts it short. Edit title/description per tool, starting from the current text, with a Revert that goes back to `server_says`; clearing both removes the override. |
| **Audit** | `GET /audit`, `GET /groups`, `GET /keys`, `GET /upstreams` | What the manager changed by itself (`events`, newest first) and the tool names renamed now; beside it, what nobody uses: groups with no keys or no used keys, keys never used, enabled servers with no calls in the stats window. Link to it from the overview when something is there. |
| **Statistics** | `GET /stats/detail?days=N`, `GET /stats` | Period toggle; totals, success rate, p50/p95; calls per day split by outcome (`stats.days`); a weekday × hour grid from `heat`; per-server, per-tool and per-app tables; the commonest chains; each app's adaptive picks (marked as listed only when its `mode` is adaptive). |
| **Search tester** | `POST /search` | Query box; show `mode`, ranked results with scores and arguments — useful for checking that descriptions make tools findable. |

### Minimal client code

JavaScript (browser):

```js
const BASE = "http://<host>:8899/api/v1";
let token = "";                       // pasted by the admin at runtime

async function api(method, path, body, timeoutMs = 10000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(BASE + path, {
      method, signal: ctl.signal,
      headers: {"authorization": "Bearer " + token, "content-type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  } finally { clearTimeout(timer); }
}

// add a server (slow: waits for the connection attempt)
const server = await api("POST", "/upstreams", {name: "browser", url: "http://host:8000/mcp"}, 90000);
// issue a key for a new app
const {key} = await api("POST", "/keys", {group: "default", label: "my app"});
```

Python (a harness provisioning keys for its own agents):

```python
import os, httpx

api = httpx.Client(base_url="http://<host>:8899/api/v1", timeout=90,
                   headers={"authorization": f"Bearer {os.environ['MCPM_ADMIN_TOKEN']}"})

def provision(agent_name: str, group: str = "default") -> dict:
    """One key per agent, so agents never share upstream sessions."""
    r = api.post("/keys", json={"group": group, "label": agent_name})
    r.raise_for_status()
    key = r.json()["key"]                       # store it now; it is never shown again
    return {"type": "http", "url": "http://<host>:8899/mcp",
            "headers": {"Authorization": f"Bearer {key}"}}

def retire(key_id: int):
    api.post(f"/keys/{key_id}/revoke").raise_for_status()
```

---

## 6. Behaviour worth knowing when explaining it to a user

- **Outages:** a down server stays listed; calls to it say it is down and that the manager keeps
  reconnecting. When it is back, the same call works and running conversations are told. A tool
  call may run up to 10 minutes; a server that stops answering mid-call is detected in seconds.
- **Isolation:** one session per key per server — or per group, on a server marked `shared`. A
  session opens on its first call to that server and closes after 15 idle minutes, or at once when
  its key (or, shared, its group) is revoked or deleted, or the server is removed or goes down. Servers that start something heavy per session (a browser, a
  worker) do so per key.
- **Token cost:** most clients send the tool list with every request. Measured on 10 servers and
  407 tools (name, description and schema): direct 115,221 tokens, adaptive with 10 learned tools
  4,351, hybrid with no defaults 640. One `search_tools` result was 316.
- **Security:** the manager can be the one secure endpoint for servers with no login and plain HTTP.
  Keep those servers on localhost or a private network; apps then need a key, and HTTPS is available.
- **Tool descriptions:** an override changes what the model reads and what search matches, and the
  server does not change. It can also shorten a long description to save tokens.
- **A group's mode is a view; its `servers` are a limit.** A hybrid key reaches every tool of its
  group's servers through `call_tool`, listed or not. A server outside `servers` is out of reach.
- **Search** uses an embedding model running inside the manager (EmbeddingGemma, embeddinggemma-300m,
  on onnxruntime) — no external service. A tool is matched on its name, title, the start of its
  description, and its server's name and description, so good server descriptions make search better.
- **Names** pass through unchanged; only collisions are prefixed `server__tool` (the server name
  made safe for clients: `_` for spaces). Defaults follow; the audit records it.
- **State** is one SQLite file. Back it up to keep servers, groups, keys and overrides.

## 7. Limits

No OAuth (API keys only) · upstreams must be streamable HTTP (no stdio) · tools only — resources and
prompts are not routed · no per-user identity inside one app · the installer is not tested on macOS.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/api/...` → 401 | Wrong token, or an app key used instead of the admin token. |
| `/api/...` → 503 | `MCPM_ADMIN_TOKEN` is not set on the manager. |
| `/mcp` → 401 | Key unknown, revoked or deleted. Re-issue it (new secret, same key) or create one. |
| Client fails the TLS handshake / `wrong version number` | The manager is plain HTTP there: TLS is on only when both cert and key are set. The startup log says `MCP on https://…` or `http://…`. |
| `CERTIFICATE_VERIFY_FAILED` / "cert invalid" against your HTTPS manager | The client must trust the root: run the installer's `--trust <file>` on that device, or pass the file as `SSL_CERT_FILE` / `--cacert` / `NODE_EXTRA_CA_CERTS`. The connected name must also be one the certificate lists: run the installer again with `--name`, or after an address changes. Easier: put Tailscale Serve or a Caddy/Let's Encrypt proxy in front and the client sees a real certificate. |
| Connection refused from another device | The manager answers on this computer only by default. Run the installer with `--network`, or set `MCPM_PUBLISH=0.0.0.0` (Docker) or `MCPM_HOST` (bare install). |
| Server stays `down` | The URL is not reachable *from the manager* (in Docker, `localhost` is the container). Check `error`. |
| A default tool is missing from an app | It is in the group's `unresolved` list: its server is down, or the name changed. |
| Search `mode` is `lexical` | The embedding model is still downloading or loading (first start), or embeddings are disabled. |
| An app never sees new tools | Expected for clients that never re-fetch their tool list: in hybrid mode everything stays reachable through `search_tools` and `call_tool`. |
| Edits in `config.yaml` have no effect | The seed only applies to an empty database. Use the API or admin page. |
