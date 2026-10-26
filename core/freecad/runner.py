"""Running Python inside FreeCAD, headlessly.

`freecadcmd.exe` is FreeCAD's console binary: it boots the full geometry kernel
without a GUI and executes a script. That makes it a clean batch backend -- no
window, no user interaction -- at the cost of being *stateless*: the process
exits when the script ends, so nothing survives between calls except what was
written to disk.

SimKit leans into that. A FreeCAD "session" is a `.FCStd` file in the output
directory; every operation opens it, changes it, recomputes and saves it. Two
consecutive tool calls therefore compose exactly like two lines of a script,
and a conversation can never act on a stale in-memory document.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import config
from .. import paths

#: stdout also carries FreeCAD's startup banner and any print() from the user's
#: own code, so the payload is fenced rather than parsed as whole-stdout JSON.
BEGIN = "<<<SIMKIT_JSON"
END = "SIMKIT_JSON>>>"

PREAMBLE = f'''
import json, os, sys, math, traceback
import FreeCAD as App
import Part

_result = {{}}

def _doc(path, create=True):
    """Open a .FCStd, or create it if it does not exist yet."""
    path = os.path.abspath(path)
    if os.path.exists(path):
        return App.openDocument(path)
    if not create:
        raise FileNotFoundError("no such FreeCAD document: " + path)
    name = os.path.splitext(os.path.basename(path))[0]
    doc = App.newDocument(name)
    doc.FileName = path
    return doc

def _save(doc):
    os.makedirs(os.path.dirname(doc.FileName), exist_ok=True)
    doc.recompute()
    doc.save()

def _obj(doc, label):
    """Find an object by Name or Label; Labels are what a user actually sees."""
    for o in doc.Objects:
        if o.Name == label or o.Label == label:
            return o
    known = ", ".join(o.Label for o in doc.Objects) or "(document is empty)"
    raise KeyError("no object %r in this document. Present: %s" % (label, known))

def _shape_props(shape):
    """The numbers that make a solid checkable: volume, area, extent, mass props."""
    bb = shape.BoundBox
    out = {{
        "volume_mm3": round(shape.Volume, 6),
        "area_mm2": round(shape.Area, 6),
        "bbox_mm": {{
            "x": round(bb.XLength, 6), "y": round(bb.YLength, 6),
            "z": round(bb.ZLength, 6),
            "min": [round(bb.XMin, 6), round(bb.YMin, 6), round(bb.ZMin, 6)],
            "max": [round(bb.XMax, 6), round(bb.YMax, 6), round(bb.ZMax, 6)],
        }},
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
        "valid": bool(shape.isValid()),
        "closed": bool(shape.isClosed()) if shape.Solids else False,
    }}
    try:
        c = shape.CenterOfMass
        out["center_of_mass_mm"] = [round(c.x, 6), round(c.y, 6), round(c.z, 6)]
    except Exception:
        pass
    return out
'''

EPILOGUE = f'''
_payload = {{"ok": True, "result": _result}}
sys.stdout.write("\\n{BEGIN}" + json.dumps(_payload, default=str) + "{END}\\n")
sys.stdout.flush()
'''

ERROR_WRAP = f'''
except Exception as _exc:
    _payload = {{
        "ok": False,
        "error": type(_exc).__name__ + ": " + str(_exc),
        "traceback": traceback.format_exc(),
    }}
    sys.stdout.write("\\n{BEGIN}" + json.dumps(_payload) + "{END}\\n")
    sys.stdout.flush()
'''


def _indent(code: str) -> str:
    return "\n".join(("    " + line) if line.strip() else line
                     for line in code.splitlines())


def build_script(body: str) -> str:
    """Wrap a snippet so its result comes back as fenced JSON, errors included."""
    return "\n".join([PREAMBLE, "try:", _indent(body), _indent(EPILOGUE), ERROR_WRAP])


def run(body: str, timeout: float | None = None) -> dict:
    """Execute `body` inside FreeCAD and return whatever it put in `_result`.

    The snippet runs with `App`, `Part`, and the `_doc`/`_save`/`_obj`/
    `_shape_props` helpers already in scope, and reports back by assigning into
    the `_result` dict.
    """
    exe = paths.find_freecad().require().path
    script = build_script(body)

    # delete=False because FreeCAD reopens the path by name on Windows, where a
    # still-open handle would block it.
    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="simkit_fc_", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        try:
            proc = subprocess.run(
                [exe, script_path],
                capture_output=True,
                text=True,
                timeout=timeout or config.FREECAD_TIMEOUT,
                cwd=config.out_dir(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"FreeCAD did not finish within {timeout or config.FREECAD_TIMEOUT:.0f}s. "
                "Booleans on dense meshes are the usual cause; raise "
                "SIMKIT_FREECAD_TIMEOUT if the model is genuinely that large."
            )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    payload = _extract(proc.stdout)
    if payload is None:
        # No fence at all means FreeCAD died before the script could report --
        # a bad install, or a syntax error in generated code.
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "FreeCAD produced no result"
            + (f" (exit code {proc.returncode})" if proc.returncode else "")
            + (f":\n{detail[-2000:]}" if detail else ".")
        )
    if not payload.get("ok"):
        raise RuntimeError("FreeCAD error -- " + payload.get("error", "unknown"))
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
