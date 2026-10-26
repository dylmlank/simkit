"""Fusion operations, as typed wrappers over the bridge.

Thin by design: the real work happens inside Fusion (see
`fusion_addin/SimKit/SimKit.py`), and this module exists so callers get
argument checking and sane defaults without a round trip.

Lengths are millimetres, matching the FreeCAD connector.
"""
from __future__ import annotations

import os

import config
from . import bridge


def _out(path: str, default_ext: str) -> str:
    if not path:
        raise ValueError("a file name is required")
    if not os.path.isabs(path):
        path = os.path.join(config.out_dir(), os.path.basename(path))
    if not os.path.splitext(path)[1]:
        path += default_ext
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def ping() -> dict:
    return bridge.send("ping")


def new_document() -> dict:
    return bridge.send("new_document")


def list_bodies() -> dict:
    return bridge.send("list_bodies")


def measure(body: str) -> dict:
    return bridge.send("measure", {"body": body})


def primitive(kind: str, params: dict, name: str = "", position=None) -> dict:
    """Create a box, cylinder, tube or extruded polygon."""
    kind = kind.lower().strip()
    required = {
        "box": ("length", "width", "height"),
        "cylinder": ("radius", "height"),
        "tube": ("outer_radius", "inner_radius", "height"),
        "polygon": ("points", "height"),
    }
    if kind not in required:
        raise ValueError(
            f"unknown primitive {kind!r}. Available: {', '.join(sorted(required))}")
    missing = [key for key in required[kind] if key not in params]
    if missing:
        raise ValueError(
            f"{kind} needs {', '.join(required[kind])}; missing {', '.join(missing)}")
    payload = dict(params)
    payload["name"] = name
    payload["position"] = position or [0, 0, 0]
    return bridge.send(kind, payload)


def boolean(operation: str, base: str, tool: str, name: str = "",
            keep_tool: bool = False) -> dict:
    return bridge.send("boolean", {
        "operation": operation, "base": base, "tool": tool,
        "name": name, "keep_tool": keep_tool})


def fillet(body: str, radius: float, max_edge_length: float = 0.0) -> dict:
    return bridge.send("fillet", {
        "body": body, "radius": float(radius),
        "max_edge_length": float(max_edge_length)})


def list_parameters() -> dict:
    return bridge.send("list_parameters")


def set_parameter(name: str, expression) -> dict:
    """Drive the model from its parameters -- the point of parametric CAD.

    The expression is Fusion's own syntax, so units may be included
    (`"120 mm"`, `"tube_od / 2"`); a bare number is taken in the parameter's
    existing unit.
    """
    return bridge.send("set_parameter", {"name": name, "expression": expression})


def add_parameter(name: str, expression, units: str = "mm",
                  comment: str = "") -> dict:
    return bridge.send("add_parameter", {
        "name": name, "expression": expression, "units": units,
        "comment": comment})


def export(path: str, fmt: str = "step", body: str = "",
           quality: str = "high") -> dict:
    """Export to STEP / IGES / SAT / SMT / STL / F3D."""
    fmt = fmt.lower().lstrip(".")
    target = _out(path, "." + fmt)
    result = bridge.send("export", {
        "path": target, "format": fmt, "body": body, "quality": quality})
    result["bytes"] = os.path.getsize(target) if os.path.isfile(target) else 0
    return result


def screenshot(path: str = "fusion_view.png", width: int = 1200,
               height: int = 800, fit: bool = True) -> dict:
    """Capture the viewport, so a model can be checked by eye rather than by number."""
    target = _out(path, ".png")
    result = bridge.send("screenshot", {
        "path": target, "width": int(width), "height": int(height), "fit": fit})
    result["bytes"] = os.path.getsize(target) if os.path.isfile(target) else 0
    return result


def run_script(code: str) -> dict:
    """Escape hatch: arbitrary Fusion Python, executed on Fusion's main thread.

    `adsk`, `app`, `ui`, `design`, `root` and the `mm_to_cm`/`cm_to_mm`
    helpers are in scope; assign into `result` to return values.
    """
    return bridge.send("run_script", {"code": code})
