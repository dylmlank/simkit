"""FreeCAD operations.

A document is addressed by name, not by handle: `bracket` means
`<out>/bracket.FCStd`. Because `runner` is stateless, that file *is* the
session, and these functions are just typed wrappers that build the snippet to
run against it.

Units are millimetres and degrees throughout -- FreeCAD's own convention, and
the one every mechanical drawing standard uses.
"""
from __future__ import annotations

import os

import config
from . import runner

#: kind -> (Part factory, ordered argument names)
PRIMITIVES: dict[str, tuple[str, tuple[str, ...]]] = {
    "box": ("makeBox", ("length", "width", "height")),
    "cylinder": ("makeCylinder", ("radius", "height")),
    "sphere": ("makeSphere", ("radius",)),
    "cone": ("makeCone", ("radius1", "radius2", "height")),
    "torus": ("makeTorus", ("radius1", "radius2")),
    "wedge": ("makeWedge", ("xmin", "ymin", "zmin", "z2min", "x2min",
                            "xmax", "ymax", "zmax", "z2max", "x2max")),
}

BOOLEANS = {"cut": "cut", "union": "fuse", "fuse": "fuse",
            "intersect": "common", "common": "common"}

#: format -> (FreeCAD exporter module, whether it needs a tessellated mesh)
EXPORT_FORMATS = {
    "step": ("Import", False), "stp": ("Import", False),
    "iges": ("Import", False), "igs": ("Import", False),
    "brep": ("Part", False),
    "stl": ("Mesh", True),
    "obj": ("Mesh", True),
    "ply": ("Mesh", True),
}


def doc_path(name: str) -> str:
    """Resolve a document name to an absolute .FCStd path inside the output dir."""
    if os.path.isabs(name):
        path = name
    else:
        path = os.path.join(config.out_dir(), os.path.basename(name))
    if not path.lower().endswith(".fcstd"):
        path += ".FCStd"
    return path


def _lit(value) -> str:
    """Embed a Python value in generated code without quoting bugs.

    `repr`, not `json.dumps`: the output is Python source, and JSON spells the
    booleans `true`/`false`. It also escapes Windows path separators correctly.
    """
    return repr(value)


def _placement(position, rotation) -> str:
    """Code that applies a translation and an XYZ-Euler rotation, in degrees."""
    if not position and not rotation:
        return ""
    px, py, pz = (position or [0, 0, 0])
    rx, ry, rz = (rotation or [0, 0, 0])
    return (
        f"\n_rot = App.Rotation({_lit(float(rz))}, {_lit(float(ry))}, {_lit(float(rx))})"
        f"\n_shape.Placement = App.Placement("
        f"App.Vector({_lit(float(px))}, {_lit(float(py))}, {_lit(float(pz))}), _rot)"
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def new_document(name: str) -> dict:
    """Create an empty document (overwrites an existing file of the same name)."""
    path = doc_path(name)
    return runner.run(f"""
path = {_lit(path)}
if os.path.exists(path):
    os.remove(path)
doc = _doc(path)
_save(doc)
_result["document"] = path
""")


def document_info(name: str) -> dict:
    """List what is in a document, with the mass properties of every solid."""
    path = doc_path(name)
    return runner.run(f"""
doc = _doc({_lit(path)}, create=False)
objs = []
for o in doc.Objects:
    entry = {{"name": o.Name, "label": o.Label, "type": o.TypeId}}
    shape = getattr(o, "Shape", None)
    if shape is not None and not shape.isNull():
        entry.update(_shape_props(shape))
    objs.append(entry)
_result["document"] = doc.FileName
_result["object_count"] = len(objs)
_result["objects"] = objs
""")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def add_primitive(document: str, kind: str, params: dict,
                  name: str = "", position=None, rotation=None) -> dict:
    """Add a solid primitive. `params` keys are the ones listed in PRIMITIVES."""
    kind = kind.lower().strip()
    if kind == "tube":
        return add_tube(document, params, name, position, rotation)
    if kind not in PRIMITIVES:
        raise ValueError(
            f"unknown primitive {kind!r}. Available: "
            + ", ".join(sorted(list(PRIMITIVES) + ["tube"]))
        )
    factory, argnames = PRIMITIVES[kind]
    missing = [a for a in argnames if a not in params]
    if missing:
        raise ValueError(f"{kind} needs {', '.join(argnames)}; missing {', '.join(missing)}")
    args = ", ".join(_lit(float(params[a])) for a in argnames)
    label = name or kind.capitalize()
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))})
_shape = Part.{factory}({args}){_placement(position, rotation)}
obj = doc.addObject("Part::Feature", {_lit(label)})
obj.Label = {_lit(label)}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
""")


def add_tube(document: str, params: dict, name: str = "",
             position=None, rotation=None) -> dict:
    """A hollow cylinder -- body tubes, sleeves, pipes. Not a Part primitive."""
    for key in ("outer_radius", "inner_radius", "height"):
        if key not in params:
            raise ValueError("tube needs outer_radius, inner_radius, height")
    ro, ri, h = (float(params["outer_radius"]), float(params["inner_radius"]),
                 float(params["height"]))
    if ri >= ro:
        raise ValueError(f"inner_radius ({ri}) must be smaller than outer_radius ({ro})")
    label = name or "Tube"
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))})
_outer = Part.makeCylinder({_lit(ro)}, {_lit(h)})
_inner = Part.makeCylinder({_lit(ri)}, {_lit(h)})
_shape = _outer.cut(_inner){_placement(position, rotation)}
obj = doc.addObject("Part::Feature", {_lit(label)})
obj.Label = {_lit(label)}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
""")


def extrude_profile(document: str, points: list, height: float, name: str = "",
                    position=None, rotation=None) -> dict:
    """Extrude a closed 2D polygon (list of [x, y]) along +Z into a solid."""
    pts = [[float(p[0]), float(p[1])] for p in points]
    if len(pts) < 3:
        raise ValueError("a profile needs at least 3 points")
    label = name or "Extrusion"
    return runner.run(f"""
pts = [App.Vector(x, y, 0) for x, y in {_lit(pts)}]
if pts[0] != pts[-1]:
    pts.append(pts[0])
_wire = Part.makePolygon(pts)
if not _wire.isClosed():
    raise ValueError("profile did not close -- check the point order")
_shape = Part.Face(_wire).extrude(App.Vector(0, 0, {_lit(float(height))}))
{_placement(position, rotation)}
doc = _doc({_lit(doc_path(document))})
obj = doc.addObject("Part::Feature", {_lit(label)})
obj.Label = {_lit(label)}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
""")


def revolve_profile(document: str, points: list, angle: float = 360.0,
                    axis=None, name: str = "") -> dict:
    """Revolve a closed 2D polygon (in the XZ plane) about an axis -- nose cones,
    pressure vessels, anything turned on a lathe."""
    pts = [[float(p[0]), float(p[1])] for p in points]
    if len(pts) < 3:
        raise ValueError("a profile needs at least 3 points")
    ax = [float(v) for v in (axis or [0, 0, 1])]
    label = name or "Revolution"
    return runner.run(f"""
pts = [App.Vector(x, 0, z) for x, z in {_lit(pts)}]
if pts[0] != pts[-1]:
    pts.append(pts[0])
_wire = Part.makePolygon(pts)
_shape = Part.Face(_wire).revolve(
    App.Vector(0, 0, 0), App.Vector(*{_lit(ax)}), {_lit(float(angle))})
doc = _doc({_lit(doc_path(document))})
obj = doc.addObject("Part::Feature", {_lit(label)})
obj.Label = {_lit(label)}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
""")


def boolean(document: str, operation: str, base: str, tool: str,
            name: str = "", keep_originals: bool = False) -> dict:
    """Cut / union / intersect two objects."""
    op = BOOLEANS.get(operation.lower().strip())
    if op is None:
        raise ValueError(f"unknown boolean {operation!r}. Use: cut, union, intersect")
    label = name or f"{operation.capitalize()}Result"
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))}, create=False)
a = _obj(doc, {_lit(base)})
b = _obj(doc, {_lit(tool)})
_shape = a.Shape.{op}(b.Shape)
if not _shape.Solids:
    raise ValueError(
        "the {operation} produced no solid -- the shapes probably do not overlap")
obj = doc.addObject("Part::Feature", {_lit(label)})
obj.Label = {_lit(label)}
obj.Shape = _shape
if not {_lit(bool(keep_originals))}:
    doc.removeObject(a.Name)
    doc.removeObject(b.Name)
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
""")


def fillet(document: str, target: str, radius: float, chamfer: bool = False,
           max_edge_length: float = 0.0) -> dict:
    """Round (or chamfer) edges. Without a filter every edge is treated."""
    verb = "makeChamfer" if chamfer else "makeFillet"
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))}, create=False)
o = _obj(doc, {_lit(target)})
edges = list(o.Shape.Edges)
_limit = {_lit(float(max_edge_length))}
if _limit > 0:
    edges = [e for e in edges if e.Length <= _limit]
if not edges:
    raise ValueError("no edges matched the filter")
try:
    _shape = o.Shape.{verb}({_lit(float(radius))}, edges)
except Exception as exc:
    raise ValueError(
        "radius {radius} is too large for at least one edge (%s). "
        "Reduce it, or set max_edge_length to target only the small edges." % exc)
o.Shape = _shape
_save(doc)
_result["object"] = o.Label
_result["edges_treated"] = len(edges)
_result.update(_shape_props(o.Shape))
""")


def measure(document: str, target: str) -> dict:
    """Volume, surface area, bounding box, centre of mass and inertia."""
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))}, create=False)
o = _obj(doc, {_lit(target)})
_result["object"] = o.Label
_result.update(_shape_props(o.Shape))
try:
    m = o.Shape.MatrixOfInertia
    _result["inertia_mm5"] = [[round(m.A11, 4), round(m.A12, 4), round(m.A13, 4)],
                              [round(m.A21, 4), round(m.A22, 4), round(m.A23, 4)],
                              [round(m.A31, 4), round(m.A32, 4), round(m.A33, 4)]]
except Exception:
    pass
""")


# ---------------------------------------------------------------------------
# Interchange
# ---------------------------------------------------------------------------

def export(document: str, path: str = "", fmt: str = "step", target: str = "",
           linear_deflection: float = 0.1, angular_deflection: float = 0.5) -> dict:
    """Export the document (or one object) to STEP / IGES / BREP / STL / OBJ / PLY.

    The deflection arguments only apply to the mesh formats and control how
    finely curves are tessellated; 0.1 mm is fine enough for CFD surfaces and
    for printing.
    """
    fmt = fmt.lower().lstrip(".")
    if fmt not in EXPORT_FORMATS:
        raise ValueError(
            f"unknown format {fmt!r}. Available: " + ", ".join(sorted(EXPORT_FORMATS)))
    module, needs_mesh = EXPORT_FORMATS[fmt]
    if not path:
        stem = os.path.splitext(os.path.basename(doc_path(document)))[0]
        path = os.path.join(config.out_dir(), f"{stem}.{fmt}")
    elif not os.path.isabs(path):
        path = os.path.join(config.out_dir(), path)

    if needs_mesh:
        body = f"""
import Mesh, MeshPart
doc = _doc({_lit(doc_path(document))}, create=False)
targets = [_obj(doc, {_lit(target)})] if {_lit(bool(target))} else [
    o for o in doc.Objects if getattr(o, "Shape", None) is not None and o.Shape.Solids]
if not targets:
    raise ValueError("nothing solid to export in this document")
_mesh = Mesh.Mesh()
for o in targets:
    _mesh.addMesh(MeshPart.meshFromShape(
        Shape=o.Shape,
        LinearDeflection={_lit(float(linear_deflection))},
        AngularDeflection={_lit(float(angular_deflection))},
        Relative=False))
os.makedirs(os.path.dirname({_lit(path)}), exist_ok=True)
_mesh.write({_lit(path)})
_result["facets"] = _mesh.CountFacets
_result["points"] = _mesh.CountPoints
"""
    else:
        body = f"""
import Import, Part
doc = _doc({_lit(doc_path(document))}, create=False)
targets = [_obj(doc, {_lit(target)})] if {_lit(bool(target))} else [
    o for o in doc.Objects if getattr(o, "Shape", None) is not None
    and not o.Shape.isNull()]
if not targets:
    raise ValueError("nothing to export in this document")
os.makedirs(os.path.dirname({_lit(path)}), exist_ok=True)
if {_lit(module)} == "Part":
    Part.export(targets, {_lit(path)})
else:
    Import.export(targets, {_lit(path)})
_result["objects_exported"] = len(targets)
"""
    result = runner.run(body + f"""
_result["path"] = {_lit(path)}
_result["format"] = {_lit(fmt)}
_result["bytes"] = os.path.getsize({_lit(path)}) if os.path.exists({_lit(path)}) else 0
""")
    return result


def import_file(document: str, path: str, name: str = "") -> dict:
    """Import a STEP / IGES / BREP / STL file into a document."""
    if not os.path.isabs(path):
        path = os.path.join(config.out_dir(), path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in EXPORT_FORMATS:
        raise ValueError(f"cannot import .{ext}")
    label = name or os.path.splitext(os.path.basename(path))[0]
    return runner.run(f"""
doc = _doc({_lit(doc_path(document))})
_before = {{o.Name for o in doc.Objects}}
if {_lit(ext)} in ("stl", "obj", "ply"):
    import Mesh
    Mesh.insert({_lit(path)}, doc.Name)
elif {_lit(ext)} == "brep":
    _shape = Part.Shape()
    _shape.read({_lit(path)})
    o = doc.addObject("Part::Feature", {_lit(label)})
    o.Shape = _shape
else:
    import Import
    Import.insert({_lit(path)}, doc.Name)
_new = [o for o in doc.Objects if o.Name not in _before]
_save(doc)
_result["imported"] = [o.Label for o in _new]
_result["object_count"] = len(_new)
""")


def run_script(code: str, document: str = "") -> dict:
    """Escape hatch: run arbitrary FreeCAD Python.

    `App`, `Part`, and the `_doc`/`_save`/`_obj`/`_shape_props` helpers are in
    scope; assign into `_result` to return values. When `document` is given it
    is opened as `doc` first and saved afterwards.
    """
    if document:
        return runner.run(
            f"doc = _doc({_lit(doc_path(document))})\n{code}\n_save(doc)")
    return runner.run(code)
