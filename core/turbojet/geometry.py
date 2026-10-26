"""The parts you fabricate around a turbocharger core.

A turbojet built on a turbo core divides cleanly in two. The compressor wheel,
turbine wheel, shaft and bearing housing come from the turbo. Everything else
you make:

    diffuser       turns the compressor's exit swirl into pressure
    flame tube     the annular liner where combustion happens
    outer casing   the pressure shell around the flame tube
    NGV ring       turns the hot gas onto the turbine blades
    exhaust nozzle accelerates the exhaust to make thrust
    end plates     locate everything and seal the ends

Every dimension here comes from MechKit's sizing of that specific engine, so
the geometry cannot drift away from the physics. In particular the flame tube's
holes are placed from `hole_pattern()` -- the primary, secondary and dilution
air splits are what actually determine whether the flame sits where it should,
and they are not something to eyeball.

Lengths are millimetres, as everywhere in SimKit's CAD tools.
"""
from __future__ import annotations

import math

from ..freecad import ops as fc
from ..freecad import runner

#: Where each air split enters, as a fraction of flame tube length from the
#: front. Primary air makes the recirculation zone that holds the flame;
#: secondary completes combustion; dilution drops the gas to what the turbine
#: can survive. Moving these is how you move the flame.
STATIONS = {
    "primary": 0.22,
    "secondary": 0.50,
    "dilution": 0.78,
}

#: Cooling air enters as rings of small holes that lay a film along the wall.
COOLING_RINGS = (0.15, 0.40, 0.65, 0.88)


def _holes_script(wall: str, radius_mm: float, z_mm: float, count: int,
                  hole_d_mm: float, length_mm: float) -> str:
    """Radial holes evenly spaced round one wall at one axial station."""
    return f"""
for _i in range({int(count)}):
    _a = 2 * math.pi * _i / {int(count)}
    _cut = Part.makeCylinder(
        {hole_d_mm / 2.0}, {length_mm},
        App.Vector({radius_mm} * math.cos(_a), {radius_mm} * math.sin(_a), {z_mm}),
        App.Vector(math.cos(_a), math.sin(_a), 0))
    {wall} = {wall}.cut(_cut)
"""


def flame_tube(design: dict, document: str = "turbojet",
               inner_wall_holes: bool = True) -> dict:
    """The annular liner, with its air-admission holes where the physics wants them.

    The liner is two concentric tubes. Air from the compressor flows around the
    outside and enters through these holes; fuel burns in the annulus between
    them. Hole size and count come straight from the combustor's air splits.
    """
    combustor = design.get("combustor")
    pattern = design.get("hole_pattern")
    if not combustor:
        raise ValueError(
            "this design has no combustor sizing. It must come from "
            "turbojet_design, which gets it from MechKit.")
    if not pattern or "splits" not in pattern:
        raise ValueError(
            "this design has no hole pattern, so the liner cannot be drilled "
            "correctly. Rebuild the design.")

    od = float(combustor["outer_diameter_mm"])
    idm = float(combustor["inner_diameter_mm"])
    length = float(combustor["length_mm"])
    wall = float(combustor["wall_thickness_mm"])
    splits = pattern["splits"]

    # The liner sits inside the casing with an air gap for the annulus flow.
    liner_od = od - 2.0 * _annulus_gap(od)
    liner_id = idm + 2.0 * _annulus_gap(od)

    body = f"""
doc = _doc({fc.doc_path(document)!r})
_outer = Part.makeCylinder({liner_od / 2.0}, {length}).cut(
    Part.makeCylinder({liner_od / 2.0 - wall}, {length}))
_inner = Part.makeCylinder({liner_id / 2.0 + wall}, {length}).cut(
    Part.makeCylinder({liner_id / 2.0}, {length}))
"""

    counts = {}
    for split, station in STATIONS.items():
        spec = splits.get(split)
        if not spec:
            continue
        count = int(spec.get("suggested_holes") or 6)
        diameter = float(spec.get("hole_diameter_mm") or 4.0)
        z = length * station
        counts[split] = {"count": count, "diameter_mm": round(diameter, 3),
                         "axial_position_mm": round(z, 2)}
        body += _holes_script("_outer", liner_od / 2.0 - wall - 1, z, count,
                              diameter, wall + 4)
        if inner_wall_holes:
            # Air also enters from the inner annulus; splitting the flow between
            # both walls is what keeps the pattern factor even across the blade.
            body += _holes_script("_inner", liner_id / 2.0 - 1, z, count,
                                  diameter * 0.7, wall + 4)

    cooling = splits.get("cooling")
    if cooling:
        # Spread the cooling area over several rings of small holes rather than
        # a few big ones: a film needs many jets, not much jet.
        total_area = float(cooling.get("total_area_mm2") or 0.0)
        per_ring = total_area / len(COOLING_RINGS)
        hole_d = 1.5
        per_ring_count = max(6, int(per_ring / (math.pi * (hole_d / 2) ** 2)))
        counts["cooling"] = {"rings": len(COOLING_RINGS),
                             "holes_per_ring": per_ring_count,
                             "diameter_mm": hole_d}
        for fraction in COOLING_RINGS:
            body += _holes_script("_outer", liner_od / 2.0 - wall - 1,
                                  length * fraction, per_ring_count, hole_d,
                                  wall + 4)

    body += f"""
_shape = _outer.fuse(_inner)
obj = doc.addObject("Part::Feature", "FlameTube")
obj.Label = "FlameTube"
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
"""
    result = runner.run(body, timeout=600)
    result["holes"] = counts
    result["liner_od_mm"] = round(liner_od, 2)
    result["liner_id_mm"] = round(liner_id, 2)
    result["length_mm"] = round(length, 2)
    return result


def _annulus_gap(outer_diameter_mm: float) -> float:
    """Radial gap between liner and casing for the air flowing round the outside.

    About 6% of the casing diameter is the usual starting point: enough area
    that the annulus does not throttle the flow, small enough that the engine
    stays compact.
    """
    return max(3.0, 0.06 * outer_diameter_mm)


def casing(design: dict, document: str = "turbojet",
           wall_mm: float = 1.5, end_allowance_mm: float = 12.0) -> dict:
    """The pressure shell around the flame tube."""
    combustor = design["combustor"]
    od = float(combustor["outer_diameter_mm"])
    length = float(combustor["length_mm"]) + 2 * end_allowance_mm

    return _add_tube(document, "Casing", od / 2.0 + wall_mm, od / 2.0, length,
                     z=-end_allowance_mm)


def exhaust_nozzle(design: dict, document: str = "turbojet",
                   wall_mm: float = 1.2, length_mm: float = 0.0) -> dict:
    """A convergent nozzle sized to the cycle's exhaust conditions.

    The exit area is what sets the engine's operating point: too large and the
    turbine never builds pressure, too small and it overheats. This takes the
    area from the cycle rather than from a rule of thumb, and reports it so it
    can be checked against a measured build.
    """
    performance = design.get("performance") or {}
    combustor = design["combustor"]

    mass_flow = float(design.get("mass_flow_kg_s") or 0.0)
    exhaust_t = float(performance.get("exhaust_gas_temperature_K") or 900.0)
    velocity = float(performance.get("specific_thrust_Ns_kg") or 0.0)
    if velocity <= 0 and mass_flow > 0:
        velocity = float(design.get("thrust_N", 0.0)) / mass_flow

    # Density of the exhaust at atmospheric back pressure, ideal gas.
    density = 101325.0 / (287.0 * max(exhaust_t, 300.0))
    area_m2 = mass_flow / (density * velocity) if velocity > 0 else 0.0
    exit_d = 2000.0 * math.sqrt(area_m2 / math.pi) if area_m2 > 0 else 40.0

    # The nozzle starts where the gas leaves the turbine, not at the casing
    # diameter. Taking the casing figure makes the cone far longer than the
    # rest of the engine and implies a diffusing section that is not there.
    turbine = design.get("turbine") or {}
    mean_d = float(turbine.get("mean_diameter_mm") or 0.0)
    blade_h = float(turbine.get("blade_height_mm") or 0.0)
    inlet_d = (mean_d + blade_h) if mean_d else float(
        combustor["outer_diameter_mm"])
    if exit_d >= inlet_d:
        # A convergent nozzle must converge; if the cycle wants more area than
        # the turbine annulus offers, say so rather than emit an expanding cone.
        raise ValueError(
            f"the cycle needs a {exit_d:.1f} mm exit but the turbine exit is "
            f"only {inlet_d:.1f} mm across, so a convergent nozzle cannot be "
            "made. The engine is running at a pressure ratio this turbine "
            "cannot pass - lower the turbine inlet temperature or the speed.")
    if length_mm <= 0:
        # A shallow cone: steeper than about 15 degrees of half-angle and the
        # flow separates off the wall instead of following it.
        length_mm = max(20.0, (inlet_d - exit_d) / (2 * math.tan(math.radians(15))))

    body = f"""
doc = _doc({fc.doc_path(document)!r})
_outer = Part.makeCone({inlet_d / 2.0 + wall_mm}, {exit_d / 2.0 + wall_mm}, {length_mm})
_inner = Part.makeCone({inlet_d / 2.0}, {exit_d / 2.0}, {length_mm})
_shape = _outer.cut(_inner)
_shape.Placement = App.Placement(
    App.Vector(0, 0, {float(combustor['length_mm'])}), App.Rotation(0, 0, 0))
obj = doc.addObject("Part::Feature", "ExhaustNozzle")
obj.Label = "ExhaustNozzle"
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
"""
    result = runner.run(body, timeout=300)
    result["exit_diameter_mm"] = round(exit_d, 2)
    result["exit_area_mm2"] = round(area_m2 * 1e6, 1)
    result["cone_length_mm"] = round(length_mm, 2)
    result["exhaust_temperature_K"] = round(exhaust_t, 1)
    result["note"] = (
        "exit area sets the operating point. Build it adjustable if you can - "
        "a removable cone lets you tune the engine on the bench instead of "
        "guessing once in CAD.")
    return result


def vane_ring(design: dict, which: str = "ngv", document: str = "turbojet",
              vane_thickness_mm: float = 1.2,
              vane_angle_deg: float = 0.0) -> dict:
    """A ring of straight vanes -- either the diffuser or the nozzle guide vanes.

    Real vanes are cambered aerofoils. These are flat plates set at an angle,
    which is what almost every amateur build uses because they can be cut from
    sheet and welded. They cost efficiency and the design's assumed component
    efficiencies already allow for that; they do not stop the engine running.
    """
    which = which.lower()
    if which not in ("ngv", "diffuser"):
        raise ValueError("which must be 'ngv' or 'diffuser'")

    if which == "diffuser":
        spec = design.get("diffuser") or {}
        count = int(spec.get("vane_count") or 15)
        outer_d = float(spec.get("exit_diameter_mm") or 70.0)
        inner_d = outer_d / float(spec.get("radius_ratio") or 1.45)
        height = 6.0
        angle = vane_angle_deg or 65.0
        label = "DiffuserVanes"
    else:
        turbine = design.get("turbine") or {}
        count = int(turbine.get("nozzle_vane_count") or 19)
        mean_d = float(turbine.get("mean_diameter_mm") or 34.0)
        height = float(turbine.get("blade_height_mm") or 11.0)
        inner_d = mean_d - height
        outer_d = mean_d + height
        angle = vane_angle_deg or 70.0
        label = "NozzleGuideVanes"

    span = (outer_d - inner_d) / 2.0
    body = f"""
doc = _doc({fc.doc_path(document)!r})
_ring = Part.makeCylinder({outer_d / 2.0}, {height}).cut(
    Part.makeCylinder({inner_d / 2.0}, {height}))
_vanes = []
for _i in range({count}):
    _a = 2 * math.pi * _i / {count}
    _v = Part.makeBox({span}, {vane_thickness_mm}, {height},
                      App.Vector(0, -{vane_thickness_mm / 2.0}, 0))
    _v.Placement = App.Placement(
        App.Vector({inner_d / 2.0} * math.cos(_a), {inner_d / 2.0} * math.sin(_a), 0),
        App.Rotation(math.degrees(_a) + {angle}, 0, 0))
    _vanes.append(_v)
_shape = _vanes[0]
for _v in _vanes[1:]:
    _shape = _shape.fuse(_v)
obj = doc.addObject("Part::Feature", {label!r})
obj.Label = {label!r}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
"""
    result = runner.run(body, timeout=600)
    result.update({"vane_count": count, "inner_diameter_mm": round(inner_d, 2),
                   "outer_diameter_mm": round(outer_d, 2),
                   "stagger_angle_deg": angle})
    if which == "diffuser":
        result["note"] = (design.get("diffuser") or {}).get("note", "")
    return result


def _add_tube(document: str, label: str, outer_r: float, inner_r: float,
              length: float, z: float = 0.0) -> dict:
    body = f"""
doc = _doc({fc.doc_path(document)!r})
_shape = Part.makeCylinder({outer_r}, {length}).cut(
    Part.makeCylinder({inner_r}, {length}))
_shape.Placement = App.Placement(App.Vector(0, 0, {z}), App.Rotation(0, 0, 0))
obj = doc.addObject("Part::Feature", {label!r})
obj.Label = {label!r}
obj.Shape = _shape
_save(doc)
_result["object"] = obj.Label
_result.update(_shape_props(obj.Shape))
"""
    return runner.run(body, timeout=300)


def build_all(design: dict, document: str = "turbojet") -> dict:
    """Build every fabricated part into one FreeCAD document."""
    fc.new_document(document)
    parts = {}
    for name, fn in (
        ("flame_tube", lambda: flame_tube(design, document)),
        ("casing", lambda: casing(design, document)),
        ("diffuser_vanes", lambda: vane_ring(design, "diffuser", document)),
        ("nozzle_guide_vanes", lambda: vane_ring(design, "ngv", document)),
        ("exhaust_nozzle", lambda: exhaust_nozzle(design, document)),
    ):
        try:
            parts[name] = fn()
        except Exception as exc:
            parts[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"document": fc.doc_path(document), "parts": parts}
