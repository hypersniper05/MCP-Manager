"""One gate, two credentials.

  /api/*   -> admin token        held by you and the portal
  everything else (/mcp) -> a registered key    held by apps, and therefore by models

The split is the point: a key buys you a view, the admin token buys you the right to
change the rules. If they were the same secret, any client could rewrite the policy that
governs every other client — which is exactly the boundary we decided to keep.

This is the ONLY check on /api/*. The handlers do not repeat it: a second copy of an auth
check is a copy that drifts, and the test suite walks every API route without the token.

Keys are compared as SHA-256 hashes, because that is all we store. The plain secret exists
only in the response that creates it.
"""

import contextvars
import secrets

from . import db

# The gate resolves identity from the credential and hands it down; handlers read it from
# here instead of parsing headers themselves. contextvars because the SDK runs handlers on
# tasks spawned after the middleware returns, and child tasks inherit the context — which
# is also exactly how the SDK carries its own auth.
current_client = contextvars.ContextVar("mcpm_client", default=None)
current_group = contextvars.ContextVar("mcpm_group", default=None)
current_key = contextvars.ContextVar("mcpm_key", default=None)       # the key's row id


class Gate:
    def __init__(self, app, db, admin_token: str | None, require_key: bool = True):
        self.app = app
        self.db = db
        self.admin_token = admin_token
        self.require_key = require_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        header = _header(scope, "authorization")

        if path.startswith("/api/"):
            # The portal itself is open HTML; only its calls need the token. A browser
            # cannot put a bearer token on a top-level navigation, and pretending
            # otherwise means inventing a session layer for one page.
            if not self.admin_token:
                return await _deny(send, 503, "management API disabled: no admin token configured")
            if not secrets.compare_digest(header.removeprefix("Bearer ").strip(), self.admin_token):
                return await _deny(send, 401, "admin token required")
        elif path.startswith("/mcp") and self.require_key:
            row = db.group_for_key(self.db, header.removeprefix("Bearer ").strip())
            if not row:
                return await _deny(send, 401, "a registered key is required")
            current_client.set(row["label"])
            current_group.set(row["name"])
            current_key.set(row["key_id"])
            db.touch_key(self.db, row["key_id"])   # the settings page's "last used"

        return await self.app(scope, receive, send)


def _header(scope, name):
    for k, v in scope.get("headers", []):
        if k.decode().lower() == name:
            return v.decode()
    return ""


async def _deny(send, status, msg):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": f'{{"error":"{msg}"}}'.encode()})
