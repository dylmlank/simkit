"""SimKit as a standalone agent.

A manual tool-use loop over the Anthropic SDK, reading the same `TOOLS` and
`dispatch` the MCP server uses. Run it when you want SimKit without a host
application:

    uv run --python 3.12 --with anthropic --with jpype1 python agent.py \
        "Model a 50 mm sphere in FreeCAD and find its drag coefficient at 15 m/s"

Needs ANTHROPIC_API_KEY (a .env next to this file is read if present).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.tools import TOOLS, dispatch

SYSTEM = """You drive four real engineering applications through SimKit:
Autodesk Fusion 360 and FreeCAD for CAD, OpenRocket for rocket flight, and
OpenFOAM for CFD.

These are real applications operating on real files, not simulations of them.
Work accordingly:

  * Run `survey` first if anything is unclear about what is available. Fusion
    in particular must be open with the add-in running; the other three work
    headlessly.
  * Respect the units. CAD tools are millimetres. OpenRocket and OpenFOAM are
    SI - metres, m/s, radians. The stl_scale argument bridges the two when CAD
    geometry goes into a CFD case, and getting it wrong silently produces
    geometry a thousand times too large.
  * Check results rather than assuming them. After meshing, read what checkMesh
    said. After solving, read the residuals. A number that did not converge is
    not an answer.
  * Say when a result is uncertain and why - a coarse mesh, an unconverged run,
    a correlation used outside its range. Do not present an estimate as a
    measurement.
"""


def _load_env() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def run(prompt: str, max_turns: int = 40) -> str:
    _load_env()
    try:
        import anthropic
    except ImportError:
        raise SystemExit("pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in E:\\simkit\\.env or the "
            "environment.")

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]

    for _ in range(max_turns):
        response = client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            thinking={"type": "adaptive"},
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in tool_uses:
            print(f"  -> {block.name}({_brief(block.input)})", file=sys.stderr)
            output = dispatch(block.name, block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    return f"Stopped after {max_turns} turns without a final answer."


def _brief(arguments: dict, limit: int = 90) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())
    return text if len(text) <= limit else text[:limit] + "..."


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    print(run(" ".join(sys.argv[1:])))
