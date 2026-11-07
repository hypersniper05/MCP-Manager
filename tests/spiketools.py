"""spiketools — the test upstream. Each tool is chosen for what it exercises.

  get_note()      plain call, returns text            → H1 hot path, H4 text pass-through
  read_image()    returns a base64 image block        → H4 binary pass-through
  confirm_note()  needs an answer mid-call            → H3 MRTR / input_required
  slow_note()     takes seconds to answer, read-only  → a busy server is not a dead one; safe to re-run
  slow_write()    takes seconds, changes something    → a call cut off by a restart must not be re-run
  get_info()      structured output (an outputSchema) → manager notes survive structured results
  fetch_page()    always blocked                      → a failure points at another server's tool
  remember()      per-session state                   → no client ever shares another's session
  recall()        reads it back

`python spiketools.py PORT` runs it over HTTP, as an upstream a test can kill and restart.
Nothing here is production code; it exists to be poked.
"""

import asyncio
import base64

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

mcp = MCPServer("spiketools")

# 1x1 red PNG — smallest valid image, proves the bytes survive the round trip.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Confirm(BaseModel):
    approved: bool = Field(description="Whether to go ahead")


@mcp.tool()
async def get_note(text: str = "default") -> str:
    """Return the text back. The boring one."""
    return f"note: {text}"


@mcp.tool()
async def read_image() -> list:
    """Return a tiny PNG as an image content block."""
    return [TextContent(type="text", text="here is the image"),
            ImageContent(type="image", data=base64.b64encode(TINY_PNG).decode(), mimeType="image/png")]


@mcp.tool()
async def confirm_note(text: str) -> str:
    """Ask the client to confirm, then answer. Exercises MRTR."""
    result = await mcp.get_context().elicit("Confirm this note?", Confirm)
    if result.action != "accept":
        return f"declined: {text}"
    return f"confirmed ({result.data.approved}): {text}"


_memory = {}       # MCP session id -> what that session asked to remember


def _session_id(ctx: Context):
    return (ctx.headers or {}).get("mcp-session-id")


@mcp.tool()
async def remember(value: str, ctx: Context) -> str:
    """Remember a value for this session only. Exercises per-client sessions."""
    _memory[_session_id(ctx)] = value
    return f"remembered {value}"


@mcp.tool()
async def recall(ctx: Context) -> str:
    """Say what this session asked to remember."""
    return f"recalled {_memory.get(_session_id(ctx), 'nothing')}"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def slow_note(seconds: float = 5) -> str:
    """Wait the given number of seconds, then answer. Exercises a slow but healthy server."""
    await asyncio.sleep(seconds)
    return f"slow note after {seconds}s"


_written = []


@mcp.tool()
async def slow_write(value: str, seconds: float = 5) -> str:
    """Wait the given number of seconds, then record the value. A call with an effect."""
    await asyncio.sleep(seconds)
    _written.append(value)
    return f"wrote {value} after {seconds}s"


class Info(BaseModel):
    name: str
    version: int


@mcp.tool()
async def get_info() -> Info:
    """Describe this server. Returns structured output."""
    return Info(name="spiketools", version=2)


@mcp.tool()
async def fetch_page(url: str) -> str:
    """Fetch and read a web page by its URL."""
    from mcp.server.mcpserver.exceptions import ToolError    # its message reaches the client, as a real server's would
    raise ToolError(f"403 Forbidden: {url} blocked by bot detection")


if __name__ == "__main__":
    import sys
    mcp.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
