"""Running MechKit's physics in MechKit's own interpreter.

SimKit does not re-implement the turbojet cycle. Duplicating a validated model
is how two answers to the same question start to disagree, and the stale one is
never the one that looks wrong.

Importing MechKit directly is not an option, though: both kits name their
package `core` and both have a top-level `config`, so whichever is imported
second is shadowed by the first. Rather than fight that with import gymnastics,
MechKit runs as a subprocess in its own virtual environment -- which also means
SimKit never needs numpy, and MechKit's dependencies stay MechKit's problem.

Same shape as the FreeCAD runner: send a snippet, get fenced JSON back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

#: Where MechKit lives, and the interpreter that has its dependencies.
MECHKIT_PATH = os.environ.get("SIMKIT_MECHKIT", r"E:\mechkit")

BEGIN = "<<<SIMKIT_JSON"
END = "SIMKIT_JSON>>>"

PREAMBLE = f'''
import json, os, sys, math, traceback
sys.path.insert(0, {MECHKIT_PATH!r})
from core.turbojet import combustor as mk_combustor
from core.turbojet import compressor as mk_compressor
from core.turbojet import turbine as mk_turbine
from core.turbojet import design as mk_design
from core.turbojet.cycle import TurbojetCycle

params = json.loads(sys.stdin.read() or "{{}}")
_result = {{}}

def _num(value, digits=6):
    """JSON has no NaN or infinity; report those as null rather than emit junk."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, digits)

def _val(obj, name, default=None):
    """Read an attribute whether MechKit exposes it as a property or a method.

    Both styles are used across its component classes -- `mass_flow` is a
    property, `stress` and `diffuser` are methods -- and which is which is not
    something SimKit should have to track. Reading it wrong fails silently:
    float() on a bound method yields None, and a null quietly becomes a missing
    safety factor.
    """
    v = getattr(obj, name, default)
    if callable(v):
        try:
            v = v()
        except Exception:
            return default
    return v
'''


def interpreter() -> str:
    """MechKit's own Python, falling back to this one.

    Its venv has numpy; the interpreter running SimKit may not, and requiring
    it would make a CAD connector depend on a numerics stack it never uses.
    """
    candidate = os.path.join(MECHKIT_PATH, ".venv", "Scripts", "python.exe")
    if os.path.isfile(candidate):
        return candidate
    candidate = os.path.join(MECHKIT_PATH, ".venv", "bin", "python")
    if os.path.isfile(candidate):
        return candidate
    return sys.executable


def available() -> tuple[bool, str]:
    """Whether MechKit's physics can be reached, and why not if it cannot."""
    if not os.path.isdir(MECHKIT_PATH):
        return False, (
            f"MechKit is not at {MECHKIT_PATH}. The turbojet tools use its "
            "cycle, compressor, combustor and turbine models. Set "
            "SIMKIT_MECHKIT to its directory.")
    if not os.path.isdir(os.path.join(MECHKIT_PATH, "core", "turbojet")):
        return False, f"{MECHKIT_PATH} has no core/turbojet package."
    return True, ""


def run(body: str, params: dict | None = None, timeout: float = 120.0) -> dict:
    """Execute a snippet against MechKit and return whatever it put in `_result`.

    The snippet runs with MechKit's turbojet modules imported as `mk_cycle`,
    `mk_compressor`, `mk_combustor`, `mk_turbine`, `mk_design`, plus a `params`
    dict of whatever was passed in.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(why)

    script = "\n".join([
        PREAMBLE,
        "try:",
        "\n".join("    " + line if line.strip() else line
                  for line in body.splitlines()),
        f'''    sys.stdout.write("\\n{BEGIN}" + json.dumps(
        {{"ok": True, "result": _result}}, default=str) + "{END}\\n")''',
        "except Exception as exc:",
        f'''    sys.stdout.write("\\n{BEGIN}" + json.dumps({{
        "ok": False,
        "error": type(exc).__name__ + ": " + str(exc),
        "traceback": traceback.format_exc(),
    }}) + "{END}\\n")''',
    ])

    try:
        proc = subprocess.run(
            [interpreter(), "-c", script],
            input=json.dumps(params or {}),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=MECHKIT_PATH,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"MechKit did not finish within {timeout:.0f}s. A wide operating-"
            "point sweep is the usual cause; narrow it or raise the timeout.")

    payload = _extract(proc.stdout)
    if payload is None:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "MechKit produced no result"
            + (f" (exit code {proc.returncode})" if proc.returncode else "")
            + (f":\n{detail[-1500:]}" if detail else ".")
        )
    if not payload.get("ok"):
        raise RuntimeError("MechKit error -- " + payload.get("error", "unknown"))
    return payload.get("result", {})


def _extract(stdout: str) -> dict | None:
    start = stdout.rfind(BEGIN)
    if start < 0:
        return None
    end = stdout.find(END, start)
    if end < 0:
        return None
    try:
        return json.loads(stdout[start + len(BEGIN):end])
    except json.JSONDecodeError:
        return None
