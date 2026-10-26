"""SimKit as an MCP server.

Exposes the same capabilities as `agent.py`, defined once in `core/tools.py`.
The 39 tools are registered by building a real function signature for each one
from its schema, so the SDK derives the tool definition the way it does for a
hand-written tool. That is more work than assigning to the server's internals,
but it keeps working when those internals move -- which they did between MCP
SDK 1.x and 2.0.

Run with:
    uv run --python 3.12 --with mcp --with jpype1 python mcp_server.py
"""
from __future__ import annotations

import inspect
import os
import sys
from typing import Annotated, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pydantic import Field  # noqa: E402

from core.tools import TOOLS, dispatch  # noqa: E402

try:
    # MCP SDK 2.0 renamed FastMCP to MCPServer.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - depends on the installed SDK
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("simkit")

JSON_TO_PYTHON = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _annotation(prop: dict):
    """Turn one JSON-schema property into a Python annotation."""
    declared = prop.get("type")
    if isinstance(declared, list):
        # A union such as ["string", "number"] -- Fusion expressions are either.
        base = Any
    else:
        base = JSON_TO_PYTHON.get(declared, Any)
    description = prop.get("description")
    enum = prop.get("enum")
    if enum:
        description = ((description + " ") if description else "") + \
            "One of: " + ", ".join(str(e) for e in enum) + "."
    if description:
        return Annotated[base, Field(description=description)]
    return base


def _register(spec: dict) -> None:
    name = spec["name"]
    schema = spec["input_schema"]
    required = set(schema.get("required", ()))

    parameters, annotations = [], {}
    for field, prop in schema.get("properties", {}).items():
        annotation = _annotation(prop)
        annotations[field] = annotation
        parameters.append(inspect.Parameter(
            field,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=annotation,
            default=inspect.Parameter.empty if field in required else None,
        ))

    async def handler(**kwargs):
        # Optional arguments arrive as None; dropping them lets each handler's
        # own defaults apply rather than overriding them with a null.
        return dispatch(name, {k: v for k, v in kwargs.items() if v is not None})

    handler.__name__ = name
    handler.__doc__ = spec["description"]
    handler.__signature__ = inspect.Signature(parameters)
    handler.__annotations__ = annotations

    mcp.add_tool(handler, name=name, description=spec["description"],
                 structured_output=False)


for _spec in TOOLS:
    _register(_spec)


if __name__ == "__main__":
    mcp.run()
