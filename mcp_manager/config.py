"""Boot config: where to listen, where the DB is, the admin token. That's it.

Everything mutable lives in SQLite and is seeded from the config's seed section on first
run only — after that the DB is truth and edits to the seed section are ignored. The
config file is the only place a secret can be stored as a hash, so it cannot also be the
thing a UI edits.

The admin token comes from .env, not from config.yaml. A committed file is the wrong home
for a secret you can rotate, and .gitignore already draws that line. No dotenv dependency:
this is eleven lines, and rule 3 applies to me too.
"""

import os
from pathlib import Path

import yaml

DEFAULTS = {
    # Localhost only, the MCP spec's own advice for a local server and the safe default for
    # a bare install. Reaching the manager from another machine is a decision, not a default:
    # set host (or MCPM_HOST) to 0.0.0.0 or one address. Docker compose sets MCPM_HOST=0.0.0.0
    # because a port mapping only reaches that bind. For access beyond the LAN, prefer a
    # TLS-terminating proxy (Tailscale Serve, Cloudflare Tunnel, Caddy) over plain HTTP.
    "host": "127.0.0.1",
    "port": 8899,
    "db": "mcp_manager.db",
    "portal_file": "portal/index.html",
    "admin_token": None,          # unset -> the management API refuses, it does not run open
    "default_group": "default",
    "require_key_for_mcp": True,  # a wrong key does not connect
    # DNS-rebinding protection defaults OFF for us, and that is deliberate. Binding a network
    # interface is a supported mode here, and a Host list we guessed would break it; every
    # request has to carry a key or an admin token anyway -- a stronger check than a Host
    # header. List the names clients use (e.g. "myhost.tailnet.ts.net:*", "100.64.0.5:*") to
    # turn it on -- recommended once a proxy publishes the manager. Then a request for any
    # other Host is refused (421) on EVERY path: the portal and the API as well as /mcp. The
    # loopback names are always allowed, so the portal keeps working on this machine.
    # Env: MCPM_ALLOWED_HOSTS, comma-separated.
    "allowed_hosts": None,
    # TLS is uvicorn's own: set both paths and the manager itself serves HTTPS — no proxy,
    # still one container. Unset = plain HTTP. On a LAN a self-signed certificate is the
    # norm (the README has a one-liner); clients must trust it or accept the warning.
    "tls_cert": None,
    "tls_key": None,
    # The edge, for a manager that faces more than localhost (limits.py). All in-process, all
    # off nobody's path but an abuser's. trust_forwarded_for: turn ON only behind a proxy that
    # sets X-Forwarded-For (Tailscale Serve/Funnel, Caddy) -- then a caller is rate-limited and
    # locked out by its real address, which that proxy writes and a client cannot forge; left
    # OFF, the socket peer is used, which is right for a direct bind and coarse behind a proxy.
    # A genuinely local request (loopback, no proxy header) is the operator and is never limited.
    # With allowed_hosts set, X-Forwarded-For is believed only on a request addressed to one of
    # those names -- the proxy always sends the public name -- so a local relay that lets callers
    # add headers cannot use it to pose as somebody else.
    "limits": {"trust_forwarded_for": False,
               "max_body_mb": 16,            # refuse a request body larger than this
               "rate_per_minute": 240,       # per-caller request ceiling; 0 disables it
               "max_concurrency": 256,       # uvicorn refuses (503) beyond this many in-flight
               "max_auth_failures": 8,       # bad token/key tries in the window before a lockout
               "auth_window_seconds": 300,
               "ban_seconds": 300,           # first lockout; doubles per repeat
               "max_ban_seconds": 86400,     # ... capped here
               "security_headers": True},
    # Embeddings run here, in this process: embeddinggemma-300m, 4-bit (embedder.py). The model
    # files are fetched from `repo` into `dir` on first start and kept (a volume, in the
    # container). Nothing outside the manager is called for them. If the model can't load,
    # search stays lexical -- it does not fail.
    "embeddings": {"dir": "models/embeddinggemma-300m",
                   "repo": "onnx-community/embeddinggemma-300m-ONNX",
                   "file": "onnx/model_q4.onnx",
                   "tokenizer": "tokenizer.json",
                   "threads": 4, "max_tokens": 2048},
}


def _dotenv(root: Path):
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env_int(d: dict, key: str, env: str) -> None:
    v = os.environ.get(env)
    if v is not None and v.strip().lstrip("-").isdigit():
        d[key] = int(v)


def _env_bool(d: dict, key: str, env: str) -> None:
    # Blank is unset, not "false": docker compose passes every variable it lists, empty when
    # .env does not set it, and that must not quietly overrule the config file.
    v = os.environ.get(env)
    if v is not None and v.strip():
        d[key] = v.strip().lower() in ("1", "true", "yes", "on")


def load(path: str) -> tuple:
    # .env lives next to the config file it belongs to. No cwd fallback — that let the
    # repo root's live .env leak into a test run and quietly override its test token.
    _dotenv(Path(path).resolve().parent)

    raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
    boot = {**DEFAULTS, **(raw.get("boot") or {})}
    # env wins over the file, not the other way round: a committed file must never be able
    # to quietly outrank a real secret sitting in .env
    boot["admin_token"] = os.environ.get("MCPM_ADMIN_TOKEN") or boot.get("admin_token")
    # shallow merge, so a config that sets only `file` still gets threads/max_tokens
    boot["embeddings"] = {**DEFAULTS["embeddings"], **(boot.get("embeddings") or {})}
    boot["limits"] = {**DEFAULTS["limits"], **(boot.get("limits") or {})}
    # The deploy-time ones get an env override, like the bind does: a proxy and its caps are a
    # property of where the manager runs, not of the committed config.
    _env_bool(boot["limits"], "trust_forwarded_for", "MCPM_TRUST_PROXY")
    _env_int(boot["limits"], "max_body_mb", "MCPM_MAX_BODY_MB")
    _env_int(boot["limits"], "rate_per_minute", "MCPM_RATE_PER_MINUTE")
    _env_int(boot["limits"], "max_concurrency", "MCPM_MAX_CONCURRENCY")
    # The names a client uses depend on how the manager is published, so the env wins here
    # too. One string or a list, in either place; blank entries are dropped.
    if os.environ.get("MCPM_ALLOWED_HOSTS", "").strip():
        boot["allowed_hosts"] = os.environ["MCPM_ALLOWED_HOSTS"]
    hosts = boot.get("allowed_hosts") or []
    if isinstance(hosts, str):
        hosts = hosts.split(",")
    boot["allowed_hosts"] = [str(h).strip() for h in hosts if str(h).strip()] or None
    # The container sets this too: inside Docker only a 0.0.0.0 bind is reachable through
    # the port mapping, while a bare install should stay on localhost (the default).
    if os.environ.get("MCPM_HOST"):
        boot["host"] = os.environ["MCPM_HOST"]
    # The container sets these two; a file path is the one thing a Docker image cannot leave
    # baked in, because the database and the model both live on volumes.
    if os.environ.get("MCPM_DB"):
        boot["db"] = os.environ["MCPM_DB"]
    if os.environ.get("MCPM_MODELS_DIR"):
        boot["embeddings"]["dir"] = os.environ["MCPM_MODELS_DIR"]
    if os.environ.get("MCPM_TLS_CERT"):
        boot["tls_cert"] = os.environ["MCPM_TLS_CERT"]
    if os.environ.get("MCPM_TLS_KEY"):
        boot["tls_key"] = os.environ["MCPM_TLS_KEY"]
    return boot, raw


def tls_kwargs(boot: dict) -> dict:
    """uvicorn's TLS options from the boot config: both files, or nothing. Half a TLS
    config must not quietly serve plain HTTP on a network — that reads as secure and
    isn't — so a missing file stops the start with a message naming what is missing."""
    if not (boot.get("tls_cert") or boot.get("tls_key")):
        return {}
    missing = [p or "(unset)" for p in (boot.get("tls_cert"), boot.get("tls_key"))
               if not (p and Path(p).exists())]
    if missing:
        raise SystemExit("TLS is configured but these are not files: " + ", ".join(missing)
                         + " — set MCPM_TLS_CERT/MCPM_TLS_KEY (or boot.tls_cert/tls_key)"
                           " to a certificate and its key, or unset both.")
    return {"ssl_certfile": boot["tls_cert"], "ssl_keyfile": boot["tls_key"]}
