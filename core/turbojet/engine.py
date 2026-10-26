"""Closing a turbojet design around a turbocharger core.

The textbook problem runs forwards: pick a thrust, see what wheel it demands.
For a homebuilt engine that is backwards, because the wheel is the one part you
cannot make. Asking for a wheel nobody sells produces a design that is either
unbuildable or -- worse -- buildable and dangerous. MechKit's own
`design_for_thrust(60 N)` shows it: it asks for a 48 mm wheel at 160,000 rpm
with a burst margin of 1.22, and says so in its warnings.

So this module runs it the other way. You give it the core you own and how hard
you are willing to spin it; it reports the thrust that follows and names the
constraint that is actually binding.

Every number comes from MechKit, computed in MechKit's own interpreter (see
`bridge.py`). What this module adds is the turbocharger constraint and the
search over operating speed.
"""
from __future__ import annotations

from . import bridge
from . import cores as cores_mod

#: Runs inside MechKit. Builds one operating point and returns it flat.
_DESIGN = '''
core = params["core"]
rpm = float(params["rpm"])

wheel = mk_compressor.CentrifugalCompressor(
    tip_diameter=core["compressor_od_mm"] / 1000.0,
    rpm=rpm,
    eye_ratio=core["inducer_ratio"],
    efficiency=float(params["compressor_efficiency"]),
    inlet_temperature=float(params["ambient_temperature"]),
    inlet_pressure=float(params["ambient_pressure"]) * float(params["intake_recovery"]),
    material=core["wheel_material"],
)

mass_flow = float(_val(wheel, "mass_flow"))
pressure_ratio = float(_val(wheel, "pressure_ratio"))
if pressure_ratio <= 1.05:
    raise ValueError(
        "at %,.0f rpm this wheel only reaches a pressure ratio of %.2f, which "
        "is not enough compression to run a cycle" % (rpm, pressure_ratio))

cycle = TurbojetCycle(
    name=core["name"] + " turbojet",
    mass_flow=mass_flow,
    pressure_ratio=pressure_ratio,
    turbine_inlet_temperature=float(params["turbine_inlet_temperature"]),
    compressor_efficiency=float(params["compressor_efficiency"]),
    turbine_efficiency=float(params["turbine_efficiency"]),
    intake_recovery=float(params["intake_recovery"]),
    ambient_temperature=float(params["ambient_temperature"]),
    ambient_pressure=float(params["ambient_pressure"]),
)
performance = cycle.performance()

whole = mk_design.TurbojetDesign(
    name=core["name"] + " turbojet", cycle=cycle, compressor=wheel)
whole.build()

_result["rpm"] = _num(rpm, 1)
_result["mass_flow_kg_s"] = _num(mass_flow)
_result["pressure_ratio"] = _num(pressure_ratio, 4)
_result["tip_speed_m_s"] = _num(_val(wheel, "tip_speed"), 2)
_result["inducer_relative_mach"] = _num(_val(wheel, "inducer_relative_mach"), 4)
_result["compressor_power_kW"] = _num(
    (_val(wheel, "power") or 0.0) / 1000.0, 4)
_result["compressor_exit_temperature_K"] = _num(_val(wheel, "exit_temperature"), 2)
_result["performance"] = {k: _num(v) if isinstance(v, (int, float)) else v
                          for k, v in performance.items()}

# Station conditions along the gas path. The nozzle's inlet total pressure and
# temperature come from here, and they are what a flow test has to be driven
# with -- guessing them would make the CFD a test of the guess.
stations = {}
for label in ("station_2", "station_3", "station_4", "station_5"):
    st = _val(cycle, label)
    if st is None:
        continue
    entry = {}
    for field in ("total_pressure", "total_temperature",
                  "static_pressure", "static_temperature", "mach"):
        value = _num(_val(st, field))
        if value is not None:
            entry[field] = value
    if entry:
        stations[label] = entry
if stations:
    _result["stations"] = stations

stress = _val(wheel, "stress")
if isinstance(stress, dict):
    _result["disc_stress"] = stress
diffuser = _val(wheel, "diffuser")
if isinstance(diffuser, dict):
    _result["diffuser"] = diffuser

warnings = []
for label, source in (("cycle", cycle), ("engine", whole), ("compressor", wheel)):
    for w in (_val(source, "check") or []):
        warnings.append(label + ": " + str(w))
_result["warnings"] = warnings

for key, name in (("power_balance", "power_balance"), ("parts", "parts"),
                  ("shaft", "shaft")):
    value = _val(whole, name)
    if isinstance(value, (dict, list)):
        _result[key] = value

combustor = getattr(whole, "combustor", None)
if combustor is not None:
    _result["combustor"] = {
        "outer_diameter_mm": _num(_val(combustor, "outer_diameter") * 1000, 3),
        "inner_diameter_mm": _num(_val(combustor, "inner_diameter") * 1000, 3),
        "length_mm": _num(_val(combustor, "length") * 1000, 3),
        "wall_thickness_mm": _num(_val(combustor, "wall_thickness") * 1000, 3),
        "wall_material": _val(combustor, "wall_material"),
        "fuel_flow_kg_h": _num((_val(combustor, "fuel_flow") or 0.0) * 3600, 4),
        "primary_fraction": _num(_val(combustor, "primary_fraction"), 4),
        "secondary_fraction": _num(_val(combustor, "secondary_fraction"), 4),
        "cooling_fraction": _num(_val(combustor, "cooling_fraction"), 4),
    }
    holes = _val(combustor, "hole_pattern")
    if isinstance(holes, dict):
        _result["hole_pattern"] = holes

turbine = getattr(whole, "turbine", None)
if turbine is not None:
    _result["turbine"] = {
        "mean_diameter_mm": _num(_val(turbine, "mean_diameter") * 1000, 3),
        "blade_height_mm": _num(_val(turbine, "blade_height") * 1000, 3),
        "blade_count": _val(turbine, "blade_count"),
        "nozzle_vane_count": _val(turbine, "nozzle_vane_count"),
        "material": _val(turbine, "material"),
        "power_required_kW": _num(
            (_val(turbine, "power_required") or 0.0) / 1000.0, 4),
    }
'''


def design_at_rpm(core, rpm: float, turbine_inlet_temperature: float = 1050.0,
                  compressor_efficiency: float = 0.72,
                  turbine_efficiency: float = 0.78,
                  intake_recovery: float = 0.98,
                  ambient_temperature: float = 288.15,
                  ambient_pressure: float = 101325.0) -> dict:
    """Close a design with this core turning at this speed.

    The default efficiencies are deliberately below textbook values. A
    turbocharger compressor was matched to a piston engine's swallowing
    capacity, not a jet's, and here it runs with a diffuser and volute it was
    never paired with. Assuming 0.78 because a book says so is the easiest way
    to design an engine that will not self-sustain.
    """
    if isinstance(core, str):
        core = cores_mod.get(core)

    rpm = float(rpm)
    if rpm > core.max_rpm:
        raise ValueError(
            f"{rpm:,.0f} rpm is above the {core.max_rpm:,.0f} rpm rating of the "
            f"{core.name}. That figure is the manufacturer's containment limit, "
            "not a suggestion.")

    payload = {
        "core": {
            "name": core.name,
            "compressor_od_mm": core.compressor_od_mm,
            "inducer_ratio": core.inducer_ratio,
            "wheel_material": core.wheel_material,
        },
        "rpm": rpm,
        "turbine_inlet_temperature": float(turbine_inlet_temperature),
        "compressor_efficiency": float(compressor_efficiency),
        "turbine_efficiency": float(turbine_efficiency),
        "intake_recovery": float(intake_recovery),
        "ambient_temperature": float(ambient_temperature),
        "ambient_pressure": float(ambient_pressure),
    }
    result = bridge.run(_DESIGN, payload)

    performance = result.get("performance", {})
    thrust = performance.get("net_thrust_N") or performance.get("thrust_N") or 0.0

    result["core"] = core.as_dict()
    result["thrust_N"] = round(float(thrust), 3)
    result["thrust_kgf"] = round(float(thrust) / 9.80665, 3)
    result["rpm_fraction_of_limit"] = round(rpm / core.max_rpm, 4)
    result["turbine_inlet_temperature_K"] = float(turbine_inlet_temperature)
    result["warnings"] = list(core.check()) + list(result.get("warnings", []))
    result["binding_constraint"] = _binding(core, result, rpm)
    return result


def _binding(core, result: dict, rpm: float) -> str:
    """Name the thing actually limiting this engine."""
    if rpm >= core.max_rpm * 0.995:
        return (f"the {core.name}'s {core.max_rpm:,.0f} rpm rating - it is at "
                "the manufacturer's limit")
    mach = result.get("inducer_relative_mach") or 0.0
    if mach >= 0.9:
        return (f"inducer relative Mach {mach:.2f} - the wheel inlet is choking, "
                "so more speed buys very little more air")
    stress = result.get("disc_stress") or {}
    margin = stress.get("safety_factor")
    if margin is not None and float(margin) < 2.0:
        return (f"compressor disc stress - safety factor {float(margin):.2f} "
                "against burst, under the factor of 2 normally wanted")
    if any("turbine" in w and "safety" in w for w in result.get("warnings", [])):
        return "turbine blade stress at temperature"
    return ("turbine inlet temperature - more thrust needs a hotter turbine, "
            "if the metal allows it")


def max_thrust(core, turbine_inlet_temperature: float = 1050.0,
               rpm_margin: float = 0.95, steps: int = 12, **kw) -> dict:
    """Find the most thrust this core can make inside its own limits.

    `rpm_margin` holds a deliberate margin below the rating. A few percent
    overspeed on startup is normal, and the rating is where containment starts
    to matter -- not a place to sit.
    """
    if isinstance(core, str):
        core = cores_mod.get(core)

    ceiling = core.max_rpm * float(rpm_margin)
    points, failures = [], []
    for i in range(steps):
        rpm = ceiling * (0.4 + 0.6 * i / max(1, steps - 1))
        try:
            points.append(design_at_rpm(
                core, rpm, turbine_inlet_temperature, **kw))
        except (ValueError, RuntimeError) as exc:
            failures.append(f"{rpm:,.0f} rpm: {exc}")

    if not points:
        raise RuntimeError(
            f"no operating point closed for the {core.name}:\n  "
            + "\n  ".join(failures[:4]))

    best = max(points, key=lambda p: p["thrust_N"])
    return {
        "core": core.name,
        "best": best,
        "curve": [{"rpm": p["rpm"], "thrust_N": p["thrust_N"],
                   "pressure_ratio": p["pressure_ratio"],
                   "mass_flow_kg_s": p["mass_flow_kg_s"]} for p in points],
        "rpm_ceiling_used": round(ceiling),
        "skipped": failures,
    }
