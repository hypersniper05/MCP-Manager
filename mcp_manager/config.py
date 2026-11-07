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
    # DNS-rebinding protection defaults OFF for us, and that is deliberate. It exists to
    # protect a server that means to be localhost-only. Binding a network interface is a
    # supported mode here, and a fixed Host list would break it; every request has to carry
    # a key or an admin token anyway -- a stronger check than a Host header. List hosts here
    # (e.g. "100.64.0.5:*") to turn it on.
    "allowed_hosts": None,
    # TLS is uvicorn's own: set both paths and the manager itself serves HTTPS — no proxy,
    # still one container. Unset = plain HTTP. On a LAN a self-signed certificate is the
    # norm (the README has a one-liner); clients must trust it or accept the warning.
    "tls_cert": None,
    "tls_key": None,
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
