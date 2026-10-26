"""Flow-testing the parts SimKit generates, against the physics that sized them.

The value of running CFD on a homebuilt engine is not a prettier answer than
the 1-D cycle gives. It is the *disagreement*. MechKit sizes the nozzle from a
one-dimensional cycle that assumes ideal expansion and a discharge coefficient
of one. OpenFOAM solves the actual duct, with a boundary layer, a real
contraction and whatever separation the cone angle causes. Where the two agree,
the design is probably sound. Where they diverge, something is wrong -- and
finding out which on a laptop is considerably cheaper than finding out on a
test stand.

Only the nozzle is tested here, and deliberately so. The compressor and turbine
wheels come with the turbocharger; SimKit has no geometry for them and cannot
invent it. The nozzle is also the part that matters most: its exit area sets
the engine's operating point, and it is the one thing you will be tempted to
adjust by eye.
"""
from __future__ import annotations

import math

from ..openfoam import compressible, run as foam_run


def nozzle_stations(design: dict, cone_length_mm: float = 0.0,
                    inlet_length_mm: float = 15.0) -> list:
    """The internal profile of the exhaust nozzle, as (z_mm, radius_mm).

    A short straight section ahead of the cone gives the boundary layer
    somewhere to develop, so the contraction is not fed a plug-flow profile it
    would never see in the real engine.
    """
    turbine = design.get("turbine") or {}
    mean_d = float(turbine.get("mean_diameter_mm") or 0.0)
    blade_h = float(turbine.get("blade_height_mm") or 0.0)
    inlet_d = mean_d + blade_h
    if inlet_d <= 0:
        raise ValueError(
            "this design has no turbine sizing, so the nozzle inlet diameter "
            "is unknown. Rebuild the design with turbojet_design.")

    exit_area_m2 = _exit_area(design)
    exit_d = 2000.0 * math.sqrt(exit_area_m2 / math.pi)
    if exit_d >= inlet_d:
        raise ValueError(
            f"the cycle wants a {exit_d:.1f} mm exit but the turbine annulus is "
            f"only {inlet_d:.1f} mm across. A convergent nozzle cannot be made; "
            "the engine is asking this turbine to pass more than it can.")

    if cone_length_mm <= 0:
        cone_length_mm = max(20.0, (inlet_d - exit_d)
                             / (2 * math.tan(math.radians(15))))
    return [
        (0.0, inlet_d / 2.0),
        (inlet_length_mm, inlet_d / 2.0),
        (inlet_length_mm + cone_length_mm, exit_d / 2.0),
    ]


def _exit_area(design: dict) -> float:
    """Exit area in m2, from the cycle's own exhaust conditions."""
    performance = design.get("performance") or {}
    mass_flow = float(design.get("mass_flow_kg_s") or 0.0)
    exhaust_t = float(performance.get("exhaust_gas_temperature_K") or 900.0)
    velocity = float(performance.get("specific_thrust_Ns_kg") or 0.0)
    if velocity <= 0 and mass_flow > 0:
        velocity = float(design.get("thrust_N", 0.0)) / mass_flow
    if velocity <= 0 or mass_flow <= 0:
        raise ValueError("this design has no usable exhaust conditions")
    density = 101325.0 / (287.0 * max(exhaust_t, 300.0))
    return mass_flow / (density * velocity)


def nozzle_inlet_conditions(design: dict) -> tuple[float, float]:
    """Total pressure and temperature entering the nozzle.

    Taken from the cycle's turbine-exit station where available. Falling back
    to a guess would make the CFD a test of the guess rather than of the
    nozzle, so a missing station is an error, not a default.
    """
    stations = design.get("stations") or {}
    station5 = stations.get("station_5") or {}
    total_p = station5.get("total_pressure")
    total_t = station5.get("total_temperature")

    if total_t is None:
        total_t = (design.get("performance") or {}).get(
            "exhaust_gas_temperature_K")
    if total_p is None or total_t is None:
        raise ValueError(
            "the design does not carry turbine-exit conditions, so the nozzle "
            "cannot be driven with the pressure it will actually see. Rebuild "
            "it with a current turbojet_design.")
    return float(total_p), float(total_t)


def test_nozzle(design: dict, name: str = "nozzle_test",
                cells_axial: int = 160, cells_radial: int = 36,
                end_time: int = 900, timeout: float = 2400.0,
                cone_length_mm: float = 0.0) -> dict:
    """Mesh and solve the exhaust nozzle, then compare with the 1-D design."""
    stations = nozzle_stations(design, cone_length_mm)
    total_p, total_t = nozzle_inlet_conditions(design)

    case = compressible.create_nozzle_case(
        name, stations, total_pressure=total_p, total_temperature=total_t,
        cells_axial=cells_axial, cells_radial=cells_radial, end_time=end_time)

    mesh = foam_run.block_mesh(name)
    check = foam_run.check_mesh(name)
    solved = foam_run.solve(name, timeout=timeout)

    result = {
        "case": case["case"],
        "openfoam": case["openfoam"],
        "profile_mm": [{"z": round(z, 2), "radius": round(r, 3)}
                       for z, r in stations],
        "exit_diameter_mm": case["exit_diameter_mm"],
        "exit_area_mm2": case["exit_area_mm2"],
        "inlet_total_pressure_Pa": case["total_pressure_Pa"],
        "inlet_total_temperature_K": case["total_temperature_K"],
        "pressure_ratio": case["pressure_ratio"],
        "chokes": case["chokes"],
        "cells": mesh.get("cells"),
        "mesh_ok": check.get("mesh_ok"),
        "converged": solved.get("converged"),
        "iterations": solved.get("time_steps"),
        "worst_residual": solved.get("worst_residual"),
    }
    if solved.get("failed"):
        result["failed"] = True
        result["error_tail"] = solved.get("error_tail", "")
        return result

    flow = foam_run.flow_summary(name)

    # The mesh is a wedge, not the whole annulus, so every extensive quantity
    # it reports is that fraction of the real one. Forgetting this is an easy
    # way to conclude a perfectly good nozzle is 70 times too small.
    sector = 360.0 / float(case["wedge_angle_deg"])
    for patch in flow["patches"].values():
        if "mass_flow_kg_s" in patch:
            patch["mass_flow_wedge_kg_s"] = patch["mass_flow_kg_s"]
            patch["mass_flow_kg_s"] = round(
                patch["mass_flow_kg_s"] * sector, 6)
    result["patches"] = flow["patches"]
    result["wedge_to_full_annulus_factor"] = round(sector, 3)

    cfd_mass_flow = (flow["patches"].get("outlet") or {}).get("mass_flow_kg_s")
    design_mass_flow = float(design.get("mass_flow_kg_s") or 0.0)
    ideal = case["predicted_choked_mass_flow_kg_s"]

    result["comparison"] = _compare(cfd_mass_flow, design_mass_flow, ideal,
                                    case["chokes"])
    return result


def size_nozzle(design: dict, name: str = "nozzle_sizing",
                tolerance: float = 0.02, max_rounds: int = 4,
                cells_axial: int = 160, cells_radial: int = 36,
                end_time: int = 900, timeout: float = 2400.0) -> dict:
    """Find the exit area that actually passes the design mass flow.

    The 1-D cycle sizes the nozzle assuming ideal expansion. The real duct has
    a boundary layer and a vena contracta, so it passes something else -- for
    this engine, about 8% more. Rather than report that and leave you to guess
    a correction, this iterates: run the flow test, scale the area by the ratio
    of wanted to measured flow, run it again.

    Mass flow through a fixed pressure drop is very nearly proportional to
    area, so the first correction lands close and the second confirms it.
    Each round is a full CFD solve, so this takes minutes, not seconds.
    """
    target = float(design.get("mass_flow_kg_s") or 0.0)
    if target <= 0:
        raise ValueError("this design has no mass flow to match")

    stations = nozzle_stations(design)
    exit_radius = stations[-1][1]
    history = []

    for round_index in range(max_rounds):
        profile = stations[:-1] + [(stations[-1][0], exit_radius)]
        result = _run_profile(design, f"{name}_{round_index}", profile,
                              cells_axial, cells_radial, end_time, timeout)
        if result.get("failed"):
            result["rounds"] = history
            return result

        measured = result["mass_flow_kg_s"]
        error = (measured - target) / target
        history.append({
            "round": round_index,
            "exit_diameter_mm": round(2 * exit_radius, 3),
            "exit_area_mm2": round(math.pi * exit_radius ** 2, 2),
            "cfd_mass_flow_kg_s": round(measured, 6),
            "error_percent": round(100 * error, 2),
            "converged": result["converged"],
        })

        if abs(error) <= tolerance:
            return {
                "converged_on_size": True,
                "rounds": history,
                "exit_diameter_mm": round(2 * exit_radius, 3),
                "exit_area_mm2": round(math.pi * exit_radius ** 2, 2),
                "final_error_percent": round(100 * error, 2),
                "design_mass_flow_kg_s": round(target, 6),
                "case": result["case"],
                "verdict": (
                    f"an exit of {2 * exit_radius:.2f} mm passes "
                    f"{measured:.4f} kg/s, within {abs(100 * error):.1f}% of "
                    "what the cycle needs. Build this one."),
            }

        # Flow is close to proportional to area at fixed pressure ratio.
        area = math.pi * exit_radius ** 2 * (target / measured)
        exit_radius = math.sqrt(area / math.pi)
        if exit_radius >= stations[0][1]:
            return {
                "converged_on_size": False,
                "rounds": history,
                "verdict": (
                    "the nozzle would have to be wider than the turbine exit "
                    "to pass this little flow, which is not a nozzle. The "
                    "cycle is asking for something this turbine cannot do."),
            }

    return {
        "converged_on_size": False,
        "rounds": history,
        "exit_diameter_mm": round(2 * exit_radius, 3),
        "verdict": (
            f"did not settle within {max_rounds} rounds. The last error was "
            f"{history[-1]['error_percent']}%; raise max_rounds, or check that "
            "each run is converging."),
    }


def _run_profile(design: dict, name: str, profile: list, cells_axial: int,
                 cells_radial: int, end_time: int, timeout: float) -> dict:
    """One meshing-and-solving round for a given nozzle profile."""
    from ..openfoam import compressible as comp

    total_p, total_t = nozzle_inlet_conditions(design)
    case = comp.create_nozzle_case(
        name, profile, total_pressure=total_p, total_temperature=total_t,
        cells_axial=cells_axial, cells_radial=cells_radial, end_time=end_time)
    foam_run.block_mesh(name)
    solved = foam_run.solve(name, timeout=timeout)
    if solved.get("failed"):
        return {"failed": True, "error_tail": solved.get("error_tail", ""),
                "case": case["case"]}

    flow = foam_run.flow_summary(name)
    sector = 360.0 / float(case["wedge_angle_deg"])
    outlet = flow["patches"].get("outlet") or {}
    return {
        "case": case["case"],
        "mass_flow_kg_s": float(outlet.get("mass_flow_kg_s", 0.0)) * sector,
        "converged": solved.get("converged"),
        "chokes": case["chokes"],
    }


def _compare(cfd, design_value, ideal, chokes) -> dict:
    """Set the three mass flows side by side and say what the gap means."""
    out = {
        "cfd_mass_flow_kg_s": cfd,
        "design_mass_flow_kg_s": round(design_value, 6) if design_value else None,
        "ideal_choked_mass_flow_kg_s": ideal,
    }
    if not cfd or not design_value:
        out["verdict"] = "not enough data to compare"
        return out

    error = (cfd - design_value) / design_value
    out["cfd_vs_design_percent"] = round(100 * error, 2)
    if ideal:
        out["discharge_coefficient"] = round(cfd / ideal, 4)

    if abs(error) <= 0.05:
        out["verdict"] = (
            "the nozzle passes what the cycle assumed, within 5%. The exit "
            "area is right for this engine.")
    elif error > 0:
        out["verdict"] = (
            f"the nozzle passes {100 * error:.1f}% more than the cycle assumed. "
            "The engine will run at a lower pressure ratio than designed, and "
            "may not build enough pressure to self-sustain. Make the exit "
            "smaller.")
    else:
        out["verdict"] = (
            f"the nozzle passes {abs(100 * error):.1f}% less than the cycle "
            "assumed. Back pressure will be higher than designed, which raises "
            "turbine inlet temperature and risks overheating. Make the exit "
            "larger.")

    if not chokes:
        out["note"] = (
            "this nozzle is not choked at the design point, so its flow "
            "depends on back pressure. Most small turbojets run choked; if "
            "this one does not, the cycle pressure ratio is lower than a jet "
            "normally needs.")
    return out
