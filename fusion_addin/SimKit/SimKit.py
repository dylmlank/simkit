"""SimKit add-in for Autodesk Fusion.

Fusion has no headless mode and no external API, so the only way in is from
inside the running application. This add-in opens a loopback socket and turns
what arrives into Fusion API calls.

The awkward part is threading. Fusion's API may only be touched from the main
thread, but a socket server has to block on accept() somewhere else. So:

    socket thread          main thread
    -------------          -----------
    read request
    store it, fire  ---->  custom event handler runs the command
    wait on Event   <----  store result, set the Event
    write response

`fireCustomEvent` is the only supported way to hand work to Fusion's main
thread, which is why the round trip looks like this rather than a direct call.
Getting it wrong does not raise -- it corrupts the document or crashes Fusion.

Lengths crossing this boundary are millimetres, matching the rest of SimKit and
every mechanical drawing standard. Fusion's API is centimetres internally, so
every value is converted at the edge; see `_cm` and `_mm`.
"""
import json
import socket
import socketserver
import threading
import traceback

import adsk.core
import adsk.fusion

HOST = "127.0.0.1"
PORT = 9720
EVENT_ID = "SimKitCommandEvent"

_app = None
_ui = None
_server = None
_server_thread = None
_custom_event = None
_event_handler = None
_handlers = []          # kept alive; Fusion drops handlers that get collected
_pending = {}           # token -> {"request", "result", "done"}
_pending_lock = threading.Lock()
_token_counter = 0


# ---------------------------------------------------------------------------
# Units. Fusion's API speaks centimetres; SimKit speaks millimetres.
# ---------------------------------------------------------------------------

def _cm(mm):
    """Millimetres in -> centimetres for the API."""
    return float(mm) / 10.0


def _mm(cm):
    """Centimetres from the API -> millimetres out."""
    return float(cm) * 10.0


def _design():
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError(
            "the active document is not a Fusion design. Open or create a "
            "design (not a drawing or an assembly view) and try again.")
    return design


def _root():
    return _design().rootComponent


def _body(name):
    root = _root()
    for body in root.bRepBodies:
        if body.name == name:
            return body
    known = ", ".join(b.name for b in root.bRepBodies) or "(no bodies)"
    raise KeyError("no body named '%s'. Present: %s" % (name, known))


def _body_props(body):
    props = body.physicalProperties
    bb = body.boundingBox
    return {
        "name": body.name,
        "volume_mm3": round(props.volume * 1000.0, 6),   # cm3 -> mm3
        "area_mm2": round(props.area * 100.0, 6),        # cm2 -> mm2
        "mass_g": round(props.mass * 1000.0, 6),         # kg -> g
        "center_of_mass_mm": [round(_mm(props.centerOfMass.x), 6),
                              round(_mm(props.centerOfMass.y), 6),
                              round(_mm(props.centerOfMass.z), 6)],
        "bbox_mm": {
            "min": [round(_mm(bb.minPoint.x), 6), round(_mm(bb.minPoint.y), 6),
                    round(_mm(bb.minPoint.z), 6)],
            "max": [round(_mm(bb.maxPoint.x), 6), round(_mm(bb.maxPoint.y), 6),
                    round(_mm(bb.maxPoint.z), 6)],
        },
        "faces": body.faces.count,
        "edges": body.edges.count,
        "is_solid": body.isSolid,
    }


# ---------------------------------------------------------------------------
# Commands. Each runs on the main thread and returns a JSON-safe dict.
# ---------------------------------------------------------------------------

def cmd_ping(_params):
    design = adsk.fusion.Design.cast(_app.activeProduct)
    return {
        "ok": True,
        "application": "Autodesk Fusion",
        "version": _app.version,
        "document": _app.activeDocument.name if _app.activeDocument else "",
        "is_design": design is not None,
        "bodies": design.rootComponent.bRepBodies.count if design else 0,
        "units": "mm (SimKit converts to Fusion's internal cm)",
    }


def cmd_new_document(_params):
    doc = _app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    return {"document": doc.name}


def cmd_list_bodies(_params):
    return {"bodies": [_body_props(b) for b in _root().bRepBodies]}


def cmd_measure(params):
    return _body_props(_body(params["body"]))


def cmd_box(params):
    """A rectangular prism, built as a sketch and an extrude."""
    length = float(params["length"])
    width = float(params["width"])
    height = float(params["height"])
    origin = params.get("position") or [0, 0, 0]

    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    x0, y0 = _cm(origin[0]), _cm(origin[1])
    lines = sketch.sketchCurves.sketchLines
    lines.addTwoPointRectangle(
        adsk.core.Point3D.create(x0, y0, 0),
        adsk.core.Point3D.create(x0 + _cm(length), y0 + _cm(width), 0))
    body = _extrude(sketch, height, origin[2], params.get("name", ""))
    return _body_props(body)


def cmd_cylinder(params):
    radius = float(params["radius"])
    height = float(params["height"])
    origin = params.get("position") or [0, 0, 0]

    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(_cm(origin[0]), _cm(origin[1]), 0), _cm(radius))
    body = _extrude(sketch, height, origin[2], params.get("name", ""))
    return _body_props(body)


def cmd_tube(params):
    """A hollow cylinder: two concentric circles in one profile."""
    outer = float(params["outer_radius"])
    inner = float(params["inner_radius"])
    height = float(params["height"])
    if inner >= outer:
        raise ValueError("inner_radius must be smaller than outer_radius")
    origin = params.get("position") or [0, 0, 0]

    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    centre = adsk.core.Point3D.create(_cm(origin[0]), _cm(origin[1]), 0)
    circles = sketch.sketchCurves.sketchCircles
    circles.addByCenterRadius(centre, _cm(outer))
    circles.addByCenterRadius(centre, _cm(inner))
    # Two concentric circles make three profiles; the annulus is the one whose
    # area is neither the full disc nor the inner disc.
    profile = _annular_profile(sketch)
    body = _extrude_profile(profile, height, origin[2], params.get("name", ""))
    return _body_props(body)


def _annular_profile(sketch):
    profiles = sorted(sketch.profiles, key=lambda p: p.areaProperties().area)
    if len(profiles) < 2:
        raise RuntimeError("expected two profiles for a tube; got %d" % len(profiles))
    # Largest is the outer disc, smallest the inner; the ring is the outer
    # profile minus the hole, which Fusion reports as its own profile.
    for profile in profiles:
        if profile.profileLoops.count == 2:
            return profile
    return profiles[-1]


def cmd_polygon(params):
    """Extrude an arbitrary closed polygon given as [[x, y], ...] in mm."""
    points = params["points"]
    height = float(params["height"])
    if len(points) < 3:
        raise ValueError("a profile needs at least 3 points")

    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    lines = sketch.sketchCurves.sketchLines
    pts = [adsk.core.Point3D.create(_cm(p[0]), _cm(p[1]), 0) for p in points]
    for i in range(len(pts)):
        lines.addByTwoPoints(pts[i], pts[(i + 1) % len(pts)])
    body = _extrude(sketch, height, (params.get("position") or [0, 0, 0])[2],
                    params.get("name", ""))
    return _body_props(body)


def _extrude(sketch, height_mm, z_offset_mm=0.0, name=""):
    if sketch.profiles.count == 0:
        raise RuntimeError("the sketch produced no closed profile")
    profile = max(sketch.profiles, key=lambda p: p.areaProperties().area)
    return _extrude_profile(profile, height_mm, z_offset_mm, name)


def _extrude_profile(profile, height_mm, z_offset_mm=0.0, name=""):
    root = _root()
    extrudes = root.features.extrudeFeatures
    ext_input = extrudes.createInput(
        profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    distance = adsk.core.ValueInput.createByReal(_cm(height_mm))
    ext_input.setDistanceExtent(False, distance)
    feature = extrudes.add(ext_input)
    body = feature.bodies.item(0)
    if name:
        body.name = name
    if z_offset_mm:
        _translate(body, [0, 0, float(z_offset_mm)])
    return body


def _translate(body, offset_mm):
    root = _root()
    vector = adsk.core.Vector3D.create(
        _cm(offset_mm[0]), _cm(offset_mm[1]), _cm(offset_mm[2]))
    matrix = adsk.core.Matrix3D.create()
    matrix.translation = vector
    collection = adsk.core.ObjectCollection.create()
    collection.add(body)
    move_input = root.features.moveFeatures.createInput(collection, matrix)
    root.features.moveFeatures.add(move_input)


def cmd_boolean(params):
    """Cut, join or intersect two bodies."""
    operation = params["operation"].lower()
    ops = {
        "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
        "union": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }
    if operation not in ops:
        raise ValueError("unknown boolean '%s'. Use cut, union or intersect"
                         % operation)
    target = _body(params["base"])
    tool = _body(params["tool"])
    tools = adsk.core.ObjectCollection.create()
    tools.add(tool)

    combines = _root().features.combineFeatures
    combine_input = combines.createInput(target, tools)
    combine_input.operation = ops[operation]
    combine_input.isKeepToolBodies = bool(params.get("keep_tool", False))
    feature = combines.add(combine_input)
    body = feature.bodies.item(0) if feature.bodies.count else target
    if params.get("name"):
        body.name = params["name"]
    return _body_props(body)


def cmd_fillet(params):
    """Round every edge of a body, or only those shorter than a limit."""
    body = _body(params["body"])
    radius = float(params["radius"])
    limit = float(params.get("max_edge_length", 0) or 0)

    edges = adsk.core.ObjectCollection.create()
    for edge in body.edges:
        if limit <= 0 or _mm(edge.length) <= limit:
            edges.add(edge)
    if edges.count == 0:
        raise ValueError("no edges matched the filter")

    fillets = _root().features.filletFeatures
    fillet_input = fillets.createInput()
    fillet_input.addConstantRadiusEdgeSet(
        edges, adsk.core.ValueInput.createByReal(_cm(radius)), True)
    fillets.add(fillet_input)
    return dict(_body_props(_body(params["body"])), edges_treated=edges.count)


# --- parameters: the reason to use Fusion rather than a mesh modeller --------

def cmd_list_parameters(_params):
    design = _design()
    out = []
    for p in design.userParameters:
        out.append({"name": p.name, "expression": p.expression,
                    "value": p.value, "unit": p.unit, "comment": p.comment})
    return {"parameters": out, "count": len(out)}


def cmd_set_parameter(params):
    """Change a user parameter and let Fusion rebuild the model."""
    design = _design()
    name = params["name"]
    parameter = design.userParameters.itemByName(name)
    if parameter is None:
        known = ", ".join(p.name for p in design.userParameters) or "(none)"
        raise KeyError("no user parameter '%s'. Present: %s" % (name, known))
    before = parameter.expression
    parameter.expression = str(params["expression"])
    return {"parameter": name, "from": before, "to": parameter.expression,
            "value": parameter.value}


def cmd_add_parameter(params):
    design = _design()
    name = params["name"]
    units = params.get("units", "mm")
    value = adsk.core.ValueInput.createByString(str(params["expression"]))
    parameter = design.userParameters.add(
        name, value, units, params.get("comment", ""))
    return {"parameter": parameter.name, "expression": parameter.expression,
            "value": parameter.value, "unit": parameter.unit}


# --- interchange ------------------------------------------------------------

EXPORTERS = {
    "step": "createSTEPExportOptions",
    "iges": "createIGESExportOptions",
    "sat": "createSATExportOptions",
    "smt": "createSMTExportOptions",
    "f3d": "createFusionArchiveExportOptions",
}


def cmd_export(params):
    """Export the design, or one body, to STEP / IGES / SAT / STL / F3D."""
    path = params["path"]
    fmt = params.get("format", "step").lower().lstrip(".")
    manager = _design().exportManager

    if fmt == "stl":
        target = _body(params["body"]) if params.get("body") else _root()
        options = manager.createSTLExportOptions(target, path)
        quality = params.get("quality", "high").lower()
        levels = {
            "low": adsk.fusion.MeshRefinementSettings.MeshRefinementLow,
            "medium": adsk.fusion.MeshRefinementSettings.MeshRefinementMedium,
            "high": adsk.fusion.MeshRefinementSettings.MeshRefinementHigh,
        }
        options.meshRefinement = levels.get(quality, levels["high"])
    elif fmt in EXPORTERS:
        options = getattr(manager, EXPORTERS[fmt])(path)
    else:
        raise ValueError("unknown format '%s'. Available: %s"
                         % (fmt, ", ".join(sorted(list(EXPORTERS) + ["stl"]))))

    manager.execute(options)
    return {"path": path, "format": fmt}


def cmd_screenshot(params):
    """Save the viewport to a PNG so the model can actually be looked at."""
    path = params["path"]
    width = int(params.get("width", 1200))
    height = int(params.get("height", 800))
    if params.get("fit", True):
        _app.activeViewport.fit()
    ok = _app.activeViewport.saveAsImageFile(path, width, height)
    if not ok:
        raise RuntimeError("Fusion declined to save the viewport image")
    return {"path": path, "width": width, "height": height}


def cmd_run_script(params):
    """Escape hatch: run arbitrary Fusion Python on the main thread.

    `adsk`, `app`, `ui`, `design` and `root` are in scope. Assign into the
    `result` dict to return values.
    """
    scope = {
        "adsk": adsk,
        "app": _app,
        "ui": _ui,
        "design": adsk.fusion.Design.cast(_app.activeProduct),
        "root": _root() if adsk.fusion.Design.cast(_app.activeProduct) else None,
        "result": {},
        "mm_to_cm": _cm,
        "cm_to_mm": _mm,
    }
    exec(params["code"], scope)
    return scope.get("result", {})


COMMANDS = {
    "ping": cmd_ping,
    "new_document": cmd_new_document,
    "list_bodies": cmd_list_bodies,
    "measure": cmd_measure,
    "box": cmd_box,
    "cylinder": cmd_cylinder,
    "tube": cmd_tube,
    "polygon": cmd_polygon,
    "boolean": cmd_boolean,
    "fillet": cmd_fillet,
    "list_parameters": cmd_list_parameters,
    "set_parameter": cmd_set_parameter,
    "add_parameter": cmd_add_parameter,
    "export": cmd_export,
    "screenshot": cmd_screenshot,
    "run_script": cmd_run_script,
}


# ---------------------------------------------------------------------------
# Main-thread execution, driven by Fusion's custom event
# ---------------------------------------------------------------------------

class _CommandEventHandler(adsk.core.CustomEventHandler):
    """Runs on Fusion's main thread. This is the only place the API is touched."""

    def notify(self, args):
        token = args.additionalInfo
        with _pending_lock:
            slot = _pending.get(token)
        if slot is None:
            return

        try:
            request = slot["request"]
            command = request.get("command", "")
            handler = COMMANDS.get(command)
            if handler is None:
                raise ValueError(
                    "unknown command '%s'. Available: %s"
                    % (command, ", ".join(sorted(COMMANDS))))
            slot["result"] = {"ok": True, "result": handler(request.get("params") or {})}
        except Exception as exc:
            slot["result"] = {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc(),
            }
        finally:
            slot["done"].set()


class _RequestHandler(object):
    """Runs on a socket thread. Must not touch the Fusion API."""

    @staticmethod
    def execute(request, timeout=120.0):
        global _token_counter
        with _pending_lock:
            _token_counter += 1
            token = "simkit-%d" % _token_counter
            slot = {"request": request, "result": None, "done": threading.Event()}
            _pending[token] = slot
        try:
            _app.fireCustomEvent(EVENT_ID, token)
            if not slot["done"].wait(timeout):
                return {"ok": False,
                        "error": "Fusion did not respond within %.0fs. It may be "
                                 "showing a dialog, or busy rebuilding." % timeout}
            return slot["result"]
        finally:
            with _pending_lock:
                _pending.pop(token, None)


class _TCPHandler(socketserver.StreamRequestHandler):
    """One newline-delimited JSON request per connection."""

    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            request = json.loads(line.decode("utf-8"))
            response = _RequestHandler.execute(
                request, float(request.get("timeout", 120)))
        except Exception as exc:
            response = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                        "traceback": traceback.format_exc()}
        try:
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (OSError, socket.error):
            pass


def run(context):
    """Fusion calls this when the add-in starts."""
    global _app, _ui, _server, _server_thread, _custom_event, _event_handler
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        # A stale registration survives a crashed add-in; clearing it first
        # makes reloading during development reliable.
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_event = _app.registerCustomEvent(EVENT_ID)
        _event_handler = _CommandEventHandler()
        _custom_event.add(_event_handler)
        _handlers.append(_event_handler)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        _server = _Server((HOST, PORT), _TCPHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()

        _app.log("SimKit: listening on %s:%d" % (HOST, PORT))
    except Exception:
        if _ui:
            _ui.messageBox("SimKit failed to start:\n%s" % traceback.format_exc())


def stop(context):
    """Fusion calls this when the add-in is unloaded."""
    global _server, _server_thread, _custom_event, _event_handler
    try:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
        _server_thread = None
        if _custom_event is not None and _event_handler is not None:
            _custom_event.remove(_event_handler)
        _app.unregisterCustomEvent(EVENT_ID)
        _custom_event = None
        _event_handler = None
        _handlers.clear()
    except Exception:
        if _ui:
            _ui.messageBox("SimKit failed to stop cleanly:\n%s"
                           % traceback.format_exc())
