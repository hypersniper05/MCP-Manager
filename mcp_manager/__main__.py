"""Entry point: load boot config, open the DB (seeding it on first run), mount the API
and the portal behind the gate, serve MCP."""

import asyncio
import logging
from pathlib import Path

import uvicorn

from mcp.server.transport_security import TransportSecuritySettings

from . import api, config, db
from .gateway import Gateway
from .gate import Gate
from .limits import Guard, Limits
from starlette.middleware.cors import CORSMiddleware

log = logging.getLogger("mcpm")
ROOT = Path(__file__).resolve().parent.parent


async def main(config_path: str = "config.yaml"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # One INFO line per HTTP request -- every health probe, every embedding batch, every
    # upstream call -- buries the lines that matter. Our own log says what happened.
    for noisy in ("httpx2", "httpx", "mcp.client.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    boot, seed_cfg = config.load(config_path)

    database = db.connect(boot["db"])
    if db.seed(database, seed_cfg):
        log.info("first run: seeded from %s — from here on the DB is truth", config_path)

    lim = boot["limits"]
    guard = Guard(lim)

    gw = Gateway(boot, database, boot["admin_token"], str(ROOT / boot["portal_file"]))
    hosts = boot["allowed_hosts"] or []
    app = gw.build().streamable_http_app(
        custom_starlette_routes=api.ROUTES,
        # without this the MCP endpoint is localhost-only: streamable_http_app() defaults to
        # host="127.0.0.1" and installs Host-header protection allowing only localhost, so a
        # client arriving over LAN or Tailscale is refused before our own gate is consulted.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(hosts), allowed_hosts=list(hosts)))
    app.state.db = database
    app.state.gateway = gw
    app.state.admin_token = boot["admin_token"]
    app.state.portal_file = gw.portal_file
    app.state.guard = guard
    app.add_middleware(Gate, db=database, admin_token=boot["admin_token"],
                       require_key=boot["require_key_for_mcp"],
                       guard=guard, trust_forwarded_for=lim["trust_forwarded_for"])
    # Added LAST so it sits in FRONT of the gate -- and that ordering is the whole point. A
    # browser preflight (OPTIONS) carries no Authorization header, and the gate would 401 it
    # before CORS ever got the chance to answer, so a standalone portal on another origin
    # could never make a single call. Wildcard is fine because CORS is not the security
    # boundary here -- the token is; a stranger's page still cannot guess it.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    # TLS is uvicorn's own job: with a cert and a key it terminates HTTPS in this same
    # process, so a network deployment needs no proxy and rule 6 still holds.
    tls = config.tls_kwargs(boot)
    # The edge sits in front of all of it: a locked-out or too-fast caller, or an oversized
    # body, is turned away before CORS or the gate, and even that refusal carries the headers.
    app.add_middleware(Limits, guard=guard, trust_forwarded_for=lim["trust_forwarded_for"],
                       max_body_bytes=lim["max_body_mb"] * 1024 * 1024,
                       security_headers=lim["security_headers"], https=bool(tls))

    scheme = "https" if tls else "http"
    log.info("MCP on %s://%s:%d/mcp — portal on %s://%s:%d/ — API on /api/v1",
             scheme, boot["host"], boot["port"], scheme, boot["host"], boot["port"])
    # uvicorn's own ceilings back the Limits layer: a hard cap on in-flight requests (503
    # past it), a bound on how large a single header event may grow, and a keep-alive timeout
    # so an idle socket is not held open for free.
    await uvicorn.Server(uvicorn.Config(app, host=boot["host"], port=boot["port"],
                                       log_level="warning",
                                       limit_concurrency=lim["max_concurrency"] or None,
                                       h11_max_incomplete_event_size=64 * 1024,
                                       timeout_keep_alive=5, **tls)).serve()


if __name__ == "__main__":
    import sys
    asyncio.run(main(*sys.argv[1:]))
