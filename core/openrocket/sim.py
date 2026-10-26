"""Running real OpenRocket simulations.

Everything here goes through OpenRocket's own engine, so the numbers match
what the application would show for the same design: Barrowman aerodynamics,
an RK4 trajectory integrator, the ISA atmosphere and the bundled thrust curves.
SimKit does not re-derive any of it.

Results are SI: metres, m/s, seconds, kilograms.
"""
from __future__ import annotations

import os

import config
from . import jvm

#: Flight-data series worth pulling back, by OpenRocket's own name for them.
SERIES = {
    "time": "TYPE_TIME",
    "altitude": "TYPE_ALTITUDE",
    "velocity": "TYPE_VELOCITY_TOTAL",
    "acceleration": "TYPE_ACCELERATION_TOTAL",
    "mach": "TYPE_MACH_NUMBER",
    "stability": "TYPE_STABILITY",
    "mass": "TYPE_MASS",
    "thrust": "TYPE_THRUST_FORCE",
    "drag": "TYPE_DRAG_FORCE",
}


def resolve(path: str) -> str:
    """Locate a .ork, allowing a bare name inside the output directory."""
    if os.path.isabs(path):
        candidate = path
    else:
        candidate = os.path.join(config.out_dir(), os.path.basename(path))
    if not candidate.lower().endswith(".ork"):
        candidate += ".ork"
    if not os.path.isfile(candidate):
        if os.path.isfile(path):
            return os.path.abspath(path)
        raise FileNotFoundError(f"no OpenRocket design at {candidate}")
    return candidate


def _document(path: str):
    env = jvm.boot()
    return env, env["GeneralRocketLoader"](env["File"](path)).load()


def _f(value):
    """Java doubles come back as NaN for "not applicable"; JSON cannot hold it."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else round(v, 6)


def design_info(path: str) -> dict:
    """Read a design: stages, components, mass, and its saved simulations."""
    path = resolve(path)
    env, doc = _document(path)
    rocket = doc.getRocket()

    components = []

    def walk(component, depth=0):
        entry = {
            "name": str(component.getName()),
            "type": str(component.getClass().getSimpleName()),
            "depth": depth,
        }
        for attr, key in (("getLength", "length_m"), ("getMass", "mass_kg"),
                          ("getOuterRadius", "outer_radius_m")):
            if hasattr(component, attr):
                try:
                    entry[key] = _f(getattr(component, attr)())
                except Exception:
                    pass
        components.append(entry)
        for child in component.getChildren():
            walk(child, depth + 1)

    walk(rocket)

    sims = []
    for i in range(doc.getSimulationCount()):
        s = doc.getSimulation(i)
        opts = s.getOptions()
        sims.append({
            "index": i,
            "name": str(s.getName()),
            "status": str(s.getStatus()),
            "launch_rod_length_m": _f(opts.getLaunchRodLength()),
            "launch_rod_angle_rad": _f(opts.getLaunchRodAngle()),
            "launch_altitude_m": _f(opts.getLaunchAltitude()),
            "time_step_s": _f(opts.getTimeStep()),
        })

    return {
        "file": path,
        "openrocket": env["version"],
        "name": str(rocket.getName()),
        "designer": str(rocket.getDesigner() or ""),
        "stages": int(rocket.getStageCount()),
        "component_count": len(components),
        "components": components,
        "simulations": sims,
    }


def simulate(path: str, index: int = 0, series: bool = False,
             samples: int = 60, launch_rod_length: float | None = None,
             launch_rod_angle_deg: float | None = None,
             wind_speed: float | None = None,
             launch_altitude: float | None = None) -> dict:
    """Run one of the design's simulations and return the flight summary.

    The optional arguments override launch conditions for this run only; the
    file on disk is not modified.
    """
    import math

    path = resolve(path)
    env, doc = _document(path)
    count = doc.getSimulationCount()
    if count == 0:
        raise ValueError(
            f"{os.path.basename(path)} contains no simulations. Add one in "
            "OpenRocket, or use openrocket_add_simulation.")
    if not 0 <= index < count:
        raise IndexError(f"simulation {index} does not exist (file has {count})")

    sim = doc.getSimulation(index)
    opts = sim.getOptions()
    if launch_rod_length is not None:
        opts.setLaunchRodLength(float(launch_rod_length))
    if launch_rod_angle_deg is not None:
        opts.setLaunchRodAngle(math.radians(float(launch_rod_angle_deg)))
    if wind_speed is not None:
        # Renamed across releases; try each spelling rather than assume.
        for setter in ("setWindSpeedAverage", "setWindAverage"):
            if hasattr(opts, setter):
                getattr(opts, setter)(float(wind_speed))
                break
    if launch_altitude is not None:
        opts.setLaunchAltitude(float(launch_altitude))

    sim.simulate()
    data = sim.getSimulatedData()
    if data is None:
        raise RuntimeError("the simulation produced no data")

    out = {
        "file": path,
        "simulation": str(sim.getName()),
        "index": index,
        "apogee_m": _f(data.getMaxAltitude()),
        "max_velocity_m_s": _f(data.getMaxVelocity()),
        "max_acceleration_m_s2": _f(data.getMaxAcceleration()),
        "max_mach": _f(data.getMaxMachNumber()),
        "time_to_apogee_s": _f(data.getTimeToApogee()),
        "flight_time_s": _f(data.getFlightTime()),
        "ground_hit_velocity_m_s": _f(data.getGroundHitVelocity()),
        "launch_rod_velocity_m_s": _f(data.getLaunchRodVelocity()),
        "deployment_velocity_m_s": _f(data.getDeploymentVelocity()),
    }

    warnings = []
    try:
        for w in data.getWarningSet():
            warnings.append(str(w))
    except Exception:
        pass
    out["warnings"] = warnings

    if series:
        out["series"] = flight_series(env, data, samples)
    return out


def flight_series(env, data, samples: int = 60) -> dict:
    """Downsample the trajectory to a size that is readable in a conversation."""
    branch = data.getBranch(0)
    ftype = env["FlightDataType"]
    out: dict[str, list] = {}
    for label, constant in SERIES.items():
        java_type = getattr(ftype, constant, None)
        if java_type is None:
            continue
        try:
            values = branch.get(java_type)
        except Exception:
            continue
        if values is None:
            continue
        values = [_f(v) for v in values]
        n = len(values)
        if n == 0:
            continue
        if n > samples:
            step = n / float(samples)
            values = [values[min(int(i * step), n - 1)] for i in range(samples)]
        out[label] = values
    return out


def simulate_all(path: str) -> dict:
    """Run every simulation in the file -- the usual way to compare motors."""
    path = resolve(path)
    _env, doc = _document(path)
    count = doc.getSimulationCount()
    results = []
    for i in range(count):
        try:
            results.append(simulate(path, i))
        except Exception as exc:
            results.append({"index": i, "error": str(exc)})
    return {"file": path, "count": count, "results": results}
