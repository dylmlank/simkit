"""Tool definitions and dispatch, shared by the MCP server and the agent.

`TOOLS` is the Anthropic tool-schema list; `mcp_server.py` wraps the same
handlers as MCP tools. Both go through `dispatch`, so a capability is defined
once and cannot drift between the two surfaces.

Conventions that hold across every tool here:

  * Lengths are millimetres in the CAD tools (FreeCAD, Fusion), because that is
    what mechanical standards use, and metres in the simulation tools
    (OpenRocket, OpenFOAM), because that is what physics uses. Each tool says
    which it wants.
  * Nothing is silently clamped or guessed. A tool that cannot do what was
    asked raises with the reason and, where there is one, the fix.
  * Files land in the output directory and their paths are returned, so results
    can be opened, inspected, or fed to the next tool.
"""
from __future__ import annotations

import json
import os
import traceback

import config
from . import paths
from .freecad import ops as fc
from .fusion import bridge as fusion_bridge
from .fusion import ops as fusion
from .openfoam import case as foam_case
from .openfoam import run as foam_run
from .openrocket import ork, sim
from .turbojet import cores as tj_cores
from .turbojet import engine as tj_engine
from .thruster import design as thr
from .turbojet import flowtest as tj_flowtest
from .turbojet import geometry as tj_geometry


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt(value, digits: int = 4) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.{digits}g}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v, digits) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


def _lines(title: str, data: dict, skip: tuple = ()) -> str:
    out = [title]
    for key, value in data.items():
        if key in skip or value is None or value == "" or value == []:
            continue
        out.append(f"  {key.replace('_', ' '):32s} {_fmt(value)}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def survey() -> str:
    """Which of the four applications are installed and reachable."""
    out = ["SimKit connector status", ""]
    for found in paths.survey():
        mark = "OK  " if found.ok else "--  "
        out.append(f"{mark}{found.name:<12} {found.version or '':<10} {found.detail}")
        if not found.ok:
            for place in found.looked_in[:4]:
                out.append(f"                 looked in: {place}")

    ok, why = _openrocket_ready()
    if not ok:
        out.append(f"    note: OpenRocket simulation unavailable - {why}")

    fusion_state = fusion_bridge.status()
    out.append("")
    out.append("Fusion add-in: "
               + ("connected" if fusion_state.get("connected")
                  else fusion_state.get("detail", "not connected").splitlines()[0]))
    out.append(f"Output directory: {config.out_dir()}")
    return "\n".join(out)


def _openrocket_ready() -> tuple[bool, str]:
    from .openrocket import jvm
    return jvm.is_available()


# ---------------------------------------------------------------------------
# FreeCAD
# ---------------------------------------------------------------------------

def freecad_new_document(name: str) -> str:
    result = fc.new_document(name)
    return f"Created FreeCAD document\n  {result['document']}"


def freecad_add_solid(document: str, kind: str, params: dict, name: str = "",
                      position=None, rotation=None) -> str:
    result = fc.add_primitive(document, kind, params, name, position, rotation)
    return _lines(f"Added {kind} to {document}", result)


def freecad_extrude_profile(document: str, points: list, height: float,
                            name: str = "", position=None) -> str:
    result = fc.extrude_profile(document, points, height, name, position)
    return _lines(f"Extruded profile in {document}", result)


def freecad_revolve_profile(document: str, points: list, angle: float = 360.0,
                            axis=None, name: str = "") -> str:
    result = fc.revolve_profile(document, points, angle, axis, name)
    return _lines(f"Revolved profile in {document}", result)


def freecad_boolean(document: str, operation: str, base: str, tool: str,
                    name: str = "", keep_originals: bool = False) -> str:
    result = fc.boolean(document, operation, base, tool, name, keep_originals)
    return _lines(f"{operation} of {base} and {tool}", result)


def freecad_fillet(document: str, target: str, radius: float,
                   chamfer: bool = False, max_edge_length: float = 0.0) -> str:
    result = fc.fillet(document, target, radius, chamfer, max_edge_length)
    verb = "Chamfered" if chamfer else "Filleted"
    return _lines(f"{verb} {target} at {radius} mm", result)


def freecad_measure(document: str, target: str) -> str:
    return _lines(f"{target} in {document}", fc.measure(document, target))


def freecad_export(document: str, fmt: str = "step", path: str = "",
                   target: str = "", linear_deflection: float = 0.1) -> str:
    result = fc.export(document, path, fmt, target,
                       linear_deflection=linear_deflection)
    return _lines(f"Exported {document} as {fmt.upper()}", result)


def freecad_import(document: str, path: str, name: str = "") -> str:
    return _lines(f"Imported into {document}", fc.import_file(document, path, name))


def freecad_document_info(document: str) -> str:
    result = fc.document_info(document)
    out = [f"{result['document']} - {result['object_count']} object(s)"]
    for obj in result["objects"]:
        bits = [f"  {obj['label']:<24} {obj['type']}"]
        if "volume_mm3" in obj:
            bits.append(f"    volume {obj['volume_mm3']:,.3f} mm3, "
                        f"{obj.get('solids', 0)} solid(s), "
                        f"valid={obj.get('valid')}")
        out.extend(bits)
    return "\n".join(out)


def freecad_run_script(code: str, document: str = "") -> str:
    result = fc.run_script(code, document)
    return "FreeCAD script finished\n" + (
        json.dumps(result, indent=2, default=str) if result else "  (no result set)")


# ---------------------------------------------------------------------------
# Fusion 360
# ---------------------------------------------------------------------------

def fusion_install_addin() -> str:
    result = fusion_bridge.install_addin()
    return (f"Installed the SimKit add-in\n  {result['installed_to']}\n\n"
            f"{result['next_step']}")


def fusion_status() -> str:
    return _lines("Fusion connection", fusion_bridge.status())


def fusion_new_document() -> str:
    return _lines("New Fusion design", fusion.new_document())


def fusion_add_solid(kind: str, params: dict, name: str = "",
                     position=None) -> str:
    return _lines(f"Added {kind}", fusion.primitive(kind, params, name, position))


def fusion_boolean(operation: str, base: str, tool: str, name: str = "",
                   keep_tool: bool = False) -> str:
    return _lines(f"{operation} of {base} and {tool}",
                  fusion.boolean(operation, base, tool, name, keep_tool))


def fusion_fillet(body: str, radius: float, max_edge_length: float = 0.0) -> str:
    return _lines(f"Filleted {body}", fusion.fillet(body, radius, max_edge_length))


def fusion_list_bodies() -> str:
    result = fusion.list_bodies()
    bodies = result.get("bodies", [])
    if not bodies:
        return "The active Fusion design has no solid bodies."
    out = [f"{len(bodies)} body/bodies in the active design"]
    for b in bodies:
        out.append(f"  {b['name']:<24} volume {b['volume_mm3']:,.2f} mm3  "
                   f"mass {b['mass_g']:,.2f} g")
    return "\n".join(out)


def fusion_parameters() -> str:
    result = fusion.list_parameters()
    params = result.get("parameters", [])
    if not params:
        return ("The design has no user parameters. Add one with "
                "fusion_add_parameter to drive the model from a value.")
    out = [f"{len(params)} user parameter(s)"]
    for p in params:
        out.append(f"  {p['name']:<20} = {p['expression']:<16} "
                   f"({p['value']} {p['unit']}) {p.get('comment', '')}")
    return "\n".join(out)


def fusion_set_parameter(name: str, expression) -> str:
    return _lines("Parameter changed", fusion.set_parameter(name, expression))


def fusion_add_parameter(name: str, expression, units: str = "mm",
                         comment: str = "") -> str:
    return _lines("Parameter added",
                  fusion.add_parameter(name, expression, units, comment))


def fusion_export(path: str, fmt: str = "step", body: str = "") -> str:
    return _lines(f"Exported as {fmt.upper()}", fusion.export(path, fmt, body))


def fusion_screenshot(path: str = "fusion_view.png", width: int = 1200,
                      height: int = 800) -> str:
    return _lines("Viewport captured", fusion.screenshot(path, width, height))


def fusion_run_script(code: str) -> str:
    result = fusion.run_script(code)
    return "Fusion script finished\n" + (
        json.dumps(result, indent=2, default=str) if result else "  (no result set)")


# ---------------------------------------------------------------------------
# OpenRocket
# ---------------------------------------------------------------------------

def openrocket_examples(name: str = "", save_as: str = "") -> str:
    """List the bundled example designs, or copy one out to work on."""
    if not name:
        result = ork.list_examples()
        return (f"{result['count']} example designs ship with OpenRocket:\n  "
                + "\n  ".join(result["examples"]))
    result = ork.extract_example(name, save_as)
    return f"Copied example '{result['example']}'\n  {result['file']}"


def openrocket_design_info(design: str) -> str:
    info = sim.design_info(design)
    out = [f"{info['name']}  ({os.path.basename(info['file'])})",
           f"  designer   {info['designer'] or '-'}",
           f"  stages     {info['stages']}",
           f"  components {info['component_count']}",
           "  structure:"]
    for c in info["components"]:
        indent = "    " + "  " * c["depth"]
        extra = ""
        if c.get("length_m") is not None:
            extra = f"  length {c['length_m'] * 1000:,.1f} mm"
        out.append(f"{indent}{c['name']} <{c['type']}>{extra}")
    out.append(f"  simulations ({len(info['simulations'])}):")
    for s in info["simulations"]:
        out.append(f"    [{s['index']}] {s['name']:<30} rod "
                   f"{s['launch_rod_length_m']} m, status {s['status']}")
    return "\n".join(out)


def openrocket_simulate(design: str, index: int = 0, series: bool = False,
                        launch_rod_length: float = None,
                        launch_rod_angle_deg: float = None,
                        wind_speed: float = None,
                        launch_altitude: float = None) -> str:
    result = sim.simulate(design, index, series,
                          launch_rod_length=launch_rod_length,
                          launch_rod_angle_deg=launch_rod_angle_deg,
                          wind_speed=wind_speed,
                          launch_altitude=launch_altitude)
    warnings = result.pop("warnings", [])
    series_data = result.pop("series", None)
    text = _lines(f"Flight simulation - {result.pop('simulation')}", result,
                  skip=("file", "index"))
    if warnings:
        text += "\n  OpenRocket warnings:\n" + "\n".join(
            f"    ! {w}" for w in warnings)
    if series_data:
        text += (f"\n  series returned: {', '.join(series_data)} "
                 f"({len(series_data.get('time', []))} samples each)")
    return text


def openrocket_simulate_all(design: str) -> str:
    result = sim.simulate_all(design)
    out = [f"{result['count']} simulation(s) in {os.path.basename(result['file'])}"]
    for r in result["results"]:
        if "error" in r:
            out.append(f"  [{r['index']}] failed: {r['error']}")
            continue
        out.append(f"  [{r['index']}] {r['simulation']:<28} "
                   f"apogee {r['apogee_m']:>9,.1f} m   "
                   f"max v {r['max_velocity_m_s']:>7,.1f} m/s   "
                   f"mach {r['max_mach']:.3f}")
    return "\n".join(out)


def openrocket_parameters(design: str) -> str:
    result = ork.list_parameters(design)
    out = [f"{result['count']} component(s) with editable fields "
           f"(all SI: metres, radians)"]
    for c in result["components"]:
        fields = ", ".join(f"{k}={v}" for k, v in c["fields"].items())
        out.append(f"  {c['component']:<24} <{c['type']}>  {fields}")
    return "\n".join(out)


def openrocket_set_parameters(design: str, component: str, changes: dict,
                              save_as: str = "") -> str:
    result = ork.set_parameters(design, component, changes, save_as)
    out = [f"Changed {result['component']} -> {result['file']}"]
    for field, change in result["applied"].items():
        out.append(f"  {field:<20} {change['from']} -> {change['to']}")
    if result["rejected"]:
        out.append(f"  rejected: {result['rejected']}")
    return "\n".join(out)


def openrocket_sweep(design: str, component: str, field: str, values: list,
                     simulate_each: bool = True, index: int = 0) -> str:
    result = ork.sweep(design, component, field, values)
    out = [f"Swept {component}.{field} over {len(values)} value(s)"]
    if not simulate_each:
        for v in result["variants"]:
            out.append(f"  {v['value']:<12g} {v['file']}")
        return "\n".join(out)

    out.append(f"  {'value':<12} {'apogee m':>10} {'max v m/s':>11} {'max mach':>9}")
    for v in result["variants"]:
        try:
            flight = sim.simulate(v["file"], index)
        except Exception as exc:
            out.append(f"  {v['value']:<12g} failed: {exc}")
            continue
        out.append(f"  {v['value']:<12g} {flight['apogee_m']:>10,.2f} "
                   f"{flight['max_velocity_m_s']:>11,.2f} "
                   f"{flight['max_mach']:>9.4f}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# OpenFOAM
# ---------------------------------------------------------------------------

def openfoam_create_case(name: str, velocity: list, bounds: dict,
                         cells: list = None, nu: float = 1.5e-5,
                         turbulence: str = "kEpsilon", two_d: bool = False,
                         steady: bool = True, end_time: float = 500,
                         write_interval: float = 100,
                         reference_length: float = 1.0,
                         geometry_stl: str = "", surface_patch: str = "body",
                         refinement: int = 2, stl_scale: float = 0.001,
                         force_coeffs: bool = False,
                         reference_area: float = 1.0,
                         ground: bool = False) -> str:
    result = foam_case.create(
        name, velocity, bounds, cells or [40, 20, 20], nu, turbulence, two_d,
        ground, steady, end_time, write_interval,
        reference_length=reference_length, geometry_stl=geometry_stl,
        surface_patch=surface_patch, refinement=refinement,
        stl_scale=stl_scale, force_coeffs=force_coeffs,
        reference_area=reference_area)
    patches = ", ".join(f"{p['name']}({p['role']})" for p in result.pop("patches"))
    text = _lines(f"OpenFOAM case '{name}'", result)
    return text + f"\n  patches                          {patches}"


def openfoam_mesh(name: str, check: bool = True) -> str:
    out = []
    block = foam_run.block_mesh(name)
    out.append(f"blockMesh: {block['cells'] or '?'} cells")

    case_path = foam_case.case_dir(name)
    if os.path.isfile(os.path.join(case_path, "system", "snappyHexMeshDict")):
        snapped = foam_run.snappy(name)
        out.append(f"snappyHexMesh: {snapped['cells'] or '?'} cells "
                   "(geometry cut into the background mesh)")

    if check:
        checked = foam_run.check_mesh(name)
        out.append(f"checkMesh: {'OK' if checked['mesh_ok'] else 'FAILED'}"
                   f", {checked['cells']} cells")
        for failure in checked["failures"]:
            out.append(f"  ! {failure}")
    return "\n".join(out)


def openfoam_solve(name: str, timeout: float = None) -> str:
    result = foam_run.solve(name, timeout)
    note = result.pop("note", "")
    residuals = result.pop("final_initial_residuals", {})
    error_tail = result.pop("error_tail", "")
    text = _lines(f"Solved '{name}'", result, skip=("case",))
    if residuals:
        text += "\n  final initial residuals:"
        for field, value in residuals.items():
            text += f"\n    {field:<12} {value:.3e}"
    if note:
        text += f"\n  NOTE: {note}"
    if error_tail:
        text += f"\n  solver output:\n{error_tail}"
    return text


def openfoam_force_coefficients(name: str, average_last: int = 50) -> str:
    result = foam_run.force_coefficients(name, average_last)
    out = [f"Force coefficients for '{name}' "
           f"({result['samples']} samples, mean of last {result['n_averaged']})"]
    for key, value in result["mean_last_n"].items():
        out.append(f"  {key:<12} {value:>12.6f}")
    out.append(f"  file: {result['file']}")
    return "\n".join(out)


def openfoam_status(name: str) -> str:
    return _lines(f"Case '{name}'", foam_run.status(name))


def openfoam_post(name: str, function: str, time: str = "") -> str:
    return _lines(f"postProcess {function}", foam_run.post_process(name, function, time))


def openfoam_to_vtk(name: str) -> str:
    return _lines("Converted to VTK (open in ParaView)", foam_run.to_vtk(name))


# ---------------------------------------------------------------------------
# Turbojet: design around a turbocharger core, then build the parts
# ---------------------------------------------------------------------------

def turbojet_cores() -> str:
    """List the turbocharger cores a design can be built around."""
    out = ["Turbocharger cores (nominal figures - measure your actual wheel)",
           f"  {'name':<12} {'comp OD':>8} {'inducer':>8} {'turbine':>8} "
           f"{'max rpm':>10} {'tip m/s':>8}"]
    for core in tj_cores.listing():
        out.append(f"  {core['name']:<12} {core['compressor_od_mm']:>8.1f} "
                   f"{core['compressor_inducer_mm']:>8.1f} "
                   f"{core['turbine_od_mm']:>8.1f} {core['max_rpm']:>10,.0f} "
                   f"{core['tip_speed_at_max_m_s']:>8.0f}")
    for core in tj_cores.CORES.values():
        if core.notes:
            out.append(f"    {core.name}: {core.notes}")
    out.append("\nUse turbojet_design with measured_* arguments once you have "
               "calipers on your own wheel.")
    return "\n".join(out)


def _core_from_args(core: str, measured_compressor_od_mm: float = 0.0,
                    measured_inducer_mm: float = 0.0,
                    measured_turbine_od_mm: float = 0.0,
                    measured_max_rpm: float = 0.0):
    if measured_compressor_od_mm and measured_inducer_mm and measured_max_rpm:
        return tj_cores.from_measurements(
            core or "measured core", measured_compressor_od_mm,
            measured_inducer_mm,
            measured_turbine_od_mm or measured_compressor_od_mm * 0.9,
            measured_max_rpm)
    return tj_cores.get(core)


def turbojet_design(core: str = "GT2560R", rpm: float = 0.0,
                    turbine_inlet_temperature: float = 1050.0,
                    compressor_efficiency: float = 0.72,
                    turbine_efficiency: float = 0.78,
                    measured_compressor_od_mm: float = 0.0,
                    measured_inducer_mm: float = 0.0,
                    measured_turbine_od_mm: float = 0.0,
                    measured_max_rpm: float = 0.0) -> str:
    """Close a turbojet design around a turbocharger core."""
    resolved = _core_from_args(core, measured_compressor_od_mm,
                               measured_inducer_mm, measured_turbine_od_mm,
                               measured_max_rpm)
    kw = {"compressor_efficiency": compressor_efficiency,
          "turbine_efficiency": turbine_efficiency}
    if rpm:
        design = tj_engine.design_at_rpm(
            resolved, rpm, turbine_inlet_temperature, **kw)
    else:
        design = tj_engine.max_thrust(
            resolved, turbine_inlet_temperature, **kw)["best"]

    out = [f"{resolved.name} turbojet - "
           f"{design['thrust_N']:.1f} N ({design['thrust_kgf']:.2f} kgf) "
           f"at {design['rpm']:,.0f} rpm"]
    for key in ("pressure_ratio", "mass_flow_kg_s", "tip_speed_m_s",
                "inducer_relative_mach", "compressor_power_kW",
                "compressor_exit_temperature_K", "rpm_fraction_of_limit"):
        out.append(f"  {key.replace('_', ' '):32s} {_fmt(design.get(key))}")
    out.append(f"  {'binding constraint':32s} {design['binding_constraint']}")

    performance = design.get("performance") or {}
    for key in ("fuel_flow_litre_hr", "tsfc_kg_Nh", "exhaust_gas_temperature_K",
                "equivalence_ratio"):
        if key in performance:
            out.append(f"  {key.replace('_', ' '):32s} {_fmt(performance[key])}")

    balance = design.get("power_balance") or {}
    if balance:
        out.append(f"  {'power balance':32s} "
                   f"compressor {balance.get('compressor_absorbs_kW', 0):.2f} kW / "
                   f"turbine {balance.get('turbine_delivers_kW', 0):.2f} kW "
                   f"({'closed' if balance.get('closed') else 'DOES NOT CLOSE'})")

    for label, key in (("Combustor", "combustor"), ("Turbine", "turbine"),
                       ("Diffuser", "diffuser")):
        block = design.get(key)
        if block:
            out.append(f"  {label}:")
            for k, v in block.items():
                out.append(f"    {k.replace('_', ' '):30s} {_fmt(v)}")

    holes = (design.get("hole_pattern") or {}).get("splits")
    if holes:
        out.append("  Flame tube air admission:")
        for split, spec in holes.items():
            out.append(f"    {split:<12} {spec.get('suggested_holes')} holes x "
                       f"{spec.get('hole_diameter_mm', 0):.2f} mm "
                       f"({spec.get('air_fraction', 0) * 100:.0f}% of air)")

    if design.get("warnings"):
        out.append("  Warnings:")
        for w in design["warnings"]:
            out.append(f"    ! {w}")
    return "\n".join(out)


def turbojet_build(core: str = "GT2560R", document: str = "turbojet",
                   rpm: float = 0.0, turbine_inlet_temperature: float = 1050.0,
                   measured_compressor_od_mm: float = 0.0,
                   measured_inducer_mm: float = 0.0,
                   measured_turbine_od_mm: float = 0.0,
                   measured_max_rpm: float = 0.0) -> str:
    """Design the engine, then build every fabricated part as real CAD."""
    resolved = _core_from_args(core, measured_compressor_od_mm,
                               measured_inducer_mm, measured_turbine_od_mm,
                               measured_max_rpm)
    if rpm:
        design = tj_engine.design_at_rpm(resolved, rpm, turbine_inlet_temperature)
    else:
        design = tj_engine.max_thrust(resolved, turbine_inlet_temperature)["best"]

    built = tj_geometry.build_all(design, document)
    out = [f"{resolved.name} turbojet - {design['thrust_N']:.1f} N at "
           f"{design['rpm']:,.0f} rpm",
           f"  document {built['document']}"]
    for name, part in built["parts"].items():
        if "error" in part:
            out.append(f"  {name:<20} FAILED - {part['error']}")
            continue
        out.append(f"  {name:<20} volume {part.get('volume_mm3', 0):>12,.1f} mm3, "
                   f"{part.get('solids', 0)} solid(s), valid={part.get('valid')}")

    nozzle = built["parts"].get("exhaust_nozzle", {})
    if "exit_diameter_mm" in nozzle:
        out.append(f"\n  Exhaust nozzle exit {nozzle['exit_diameter_mm']} mm "
                   f"({nozzle['exit_area_mm2']} mm2) - {nozzle['note']}")
    out.append("\n  Export with freecad_export, or open the .FCStd in FreeCAD.")
    return "\n".join(out)


def turbojet_flow_test(core: str = "GT2560R", name: str = "nozzle_test",
                       rpm: float = 0.0,
                       turbine_inlet_temperature: float = 1050.0,
                       cells_axial: int = 160, cells_radial: int = 36,
                       end_time: int = 900, timeout: float = 2400.0,
                       measured_compressor_od_mm: float = 0.0,
                       measured_inducer_mm: float = 0.0,
                       measured_turbine_od_mm: float = 0.0,
                       measured_max_rpm: float = 0.0) -> str:
    """CFD the exhaust nozzle and compare it with the 1-D design."""
    resolved = _core_from_args(core, measured_compressor_od_mm,
                               measured_inducer_mm, measured_turbine_od_mm,
                               measured_max_rpm)
    if rpm:
        design = tj_engine.design_at_rpm(resolved, rpm, turbine_inlet_temperature)
    else:
        design = tj_engine.max_thrust(resolved, turbine_inlet_temperature)["best"]

    result = tj_flowtest.test_nozzle(
        design, name, cells_axial, cells_radial, end_time, timeout)

    out = [f"Nozzle flow test - {resolved.name} at {design['rpm']:,.0f} rpm",
           f"  {'geometry':30s} {result['exit_diameter_mm']} mm exit "
           f"({result['exit_area_mm2']} mm2)"]
    for key in ("openfoam", "inlet_total_pressure_Pa",
                "inlet_total_temperature_K", "pressure_ratio", "chokes",
                "cells", "mesh_ok", "converged", "iterations",
                "worst_residual", "wedge_to_full_annulus_factor"):
        if key in result:
            out.append(f"  {key.replace('_', ' '):30s} {_fmt(result[key])}")

    if result.get("failed"):
        out.append("\n  The solver failed:\n" + result.get("error_tail", ""))
        return "\n".join(out)

    for patch, values in (result.get("patches") or {}).items():
        out.append(f"  {patch}:")
        for key, value in values.items():
            if key == "time":
                continue
            out.append(f"    {key:<28} {_fmt(value)}")

    comparison = result.get("comparison") or {}
    out.append("\n  Against the 1-D design:")
    for key in ("cfd_mass_flow_kg_s", "design_mass_flow_kg_s",
                "ideal_choked_mass_flow_kg_s", "cfd_vs_design_percent",
                "discharge_coefficient"):
        if comparison.get(key) is not None:
            out.append(f"    {key.replace('_', ' '):28} {_fmt(comparison[key])}")
    if comparison.get("verdict"):
        out.append(f"\n  {comparison['verdict']}")
    if comparison.get("note"):
        out.append(f"  {comparison['note']}")
    return "\n".join(out)


def turbojet_size_nozzle(core: str = "GT2560R", name: str = "nozzle_sizing",
                         rpm: float = 0.0,
                         turbine_inlet_temperature: float = 1050.0,
                         tolerance: float = 0.02, max_rounds: int = 4,
                         end_time: int = 900, timeout: float = 2400.0,
                         measured_compressor_od_mm: float = 0.0,
                         measured_inducer_mm: float = 0.0,
                         measured_turbine_od_mm: float = 0.0,
                         measured_max_rpm: float = 0.0) -> str:
    """Iterate the nozzle exit until CFD passes the mass flow the cycle needs."""
    resolved = _core_from_args(core, measured_compressor_od_mm,
                               measured_inducer_mm, measured_turbine_od_mm,
                               measured_max_rpm)
    if rpm:
        design = tj_engine.design_at_rpm(resolved, rpm, turbine_inlet_temperature)
    else:
        design = tj_engine.max_thrust(resolved, turbine_inlet_temperature)["best"]

    result = tj_flowtest.size_nozzle(
        design, name, tolerance, max_rounds, end_time=end_time, timeout=timeout)

    out = [f"Nozzle sizing - {resolved.name} at {design['rpm']:,.0f} rpm",
           f"  target mass flow {design['mass_flow_kg_s']} kg/s (from the cycle)",
           "",
           f"  {'round':<6}{'exit mm':>10}{'area mm2':>12}"
           f"{'CFD kg/s':>12}{'error':>9}"]
    for entry in result.get("rounds", []):
        out.append(f"  {entry['round']:<6}{entry['exit_diameter_mm']:>10.3f}"
                   f"{entry['exit_area_mm2']:>12.2f}"
                   f"{entry['cfd_mass_flow_kg_s']:>12.5f}"
                   f"{entry['error_percent']:>8.2f}%")
    if result.get("failed"):
        out.append("\n  A solver run failed:\n" + result.get("error_tail", "")[:1500])
        return "\n".join(out)
    out.append("")
    out.append(f"  {result.get('verdict', '')}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Air-fed thruster
# ---------------------------------------------------------------------------

def thruster_design(supply_pressure_bar: float = 6.0, supply_cfm: float = 5.0,
                    chamber_temperature: float = 288.15,
                    divergent: bool = False,
                    tank_litres: float = 0.0, tank_pressure_bar: float = 300.0,
                    airframe_mass_kg: float = 0.0) -> str:
    """Size a small air-fed thruster around the air you can actually supply."""
    t = thr.design(supply_pressure_bar, supply_cfm=supply_cfm,
                   chamber_temperature=chamber_temperature, divergent=divergent)
    d = t.as_dict()

    out = [f"Air-fed thruster — {d['thrust_N']:.2f} N ({d['thrust_gf']:.0f} gram-force)"]
    for key in ("supply_pressure_bar_g", "supply_cfm_free_air",
                "supply_mass_flow_kg_s", "chamber_temperature_K", "nozzle",
                "throat_diameter_mm", "exit_diameter_mm", "exit_velocity_m_s",
                "choked"):
        out.append(f"  {key.replace('_', ' '):30s} {_fmt(d[key])}")

    if tank_litres > 0:
        flight = thr.can_it_fly(t.thrust_n, tank_litres, tank_pressure_bar,
                                t.supply_mass_flow, airframe_mass_kg)
        out.append(f"\n  Running from a {tank_litres} L bottle at "
                   f"{tank_pressure_bar:.0f} bar:")
        for key in ("usable_air_kg", "run_time_s", "approx_tank_mass_kg",
                    "total_mass_kg", "thrust_to_weight"):
            out.append(f"    {key.replace('_', ' '):28s} {_fmt(flight[key])}")
        out.append(f"\n  {flight['verdict']}")

    out.append("\n  Parts:")
    for part in thr.parts_list(t):
        out.append(f"    {part['part']:<22} {part['spec']}  [{part['material']}]")
        out.append(f"      {part['note']}")

    if t.warnings:
        out.append("\n  Warnings:")
        for w in t.warnings:
            out.append(f"    ! {w}")
    return "\n".join(out)


def thruster_supply_sweep(supply_pressure_bar: float = 6.0,
                          chamber_temperature: float = 288.15,
                          divergent: bool = False) -> str:
    """Thrust against air supply — the curve that decides the whole project."""
    rows = thr.supply_sweep([1, 2, 3, 5, 8, 10, 15, 20, 30],
                            supply_pressure_bar, chamber_temperature, divergent)
    out = [f"Thrust vs air supply at {supply_pressure_bar:.0f} bar, "
           f"chamber {chamber_temperature:.0f} K",
           f"  {'CFM':>5}{'kg/s':>10}{'throat mm':>11}{'thrust N':>10}{'gram-f':>9}"]
    for r in rows:
        out.append(f"  {r['supply_cfm_free_air']:>5.0f}"
                   f"{r['supply_mass_flow_kg_s']:>10.5f}"
                   f"{r['throat_diameter_mm']:>11.2f}"
                   f"{r['thrust_N']:>10.2f}{r['thrust_gf']:>9.0f}")
    out.append("\n  A hobby compressor is 2-5 CFM continuous; a good workshop "
               "unit 10-20. Rated 'displacement' is optimistic — use the "
               "delivered figure at your working pressure.")
    return "\n".join(out)


def thruster_heat_benefit(supply_cfm: float = 5.0,
                          supply_pressure_bar: float = 6.0) -> str:
    """What adding a burner buys, for a fixed air supply."""
    rows = thr.heat_benefit(supply_cfm, supply_pressure_bar,
                            temperatures=(288.15, 600, 900, 1200, 1500))
    base = rows[0]["thrust_N"]
    out = [f"Effect of heating the air — {supply_cfm:.0f} CFM at "
           f"{supply_pressure_bar:.0f} bar",
           f"  {'chamber K':>10}{'throat mm':>11}{'exit m/s':>10}"
           f"{'thrust N':>10}{'vs cold':>9}"]
    for r in rows:
        out.append(f"  {r['chamber_temperature_K']:>10.0f}"
                   f"{r['throat_diameter_mm']:>11.2f}"
                   f"{r['exit_velocity_m_s']:>10.0f}"
                   f"{r['thrust_N']:>10.2f}"
                   f"{r['thrust_N'] / base:>8.2f}x")
    out.append("\n  Exit velocity goes as the square root of temperature, so "
               "heat is the cheapest thrust once the compressor is fixed. It "
               "is also the step that turns a loud air jet into a fire risk: "
               "steel only, outdoors, extinguisher to hand.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

def _tool(name, description, properties, required=()):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
    }


_NUM = {"type": "number"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_VEC3 = {"type": "array", "items": _NUM, "minItems": 3, "maxItems": 3}

TOOLS = [
    _tool("survey",
          "Report which of the four applications (Fusion 360, FreeCAD, "
          "OpenRocket, OpenFOAM) are installed, which version, and whether "
          "each is reachable right now. Run this first when something fails.",
          {}),

    # --- FreeCAD ---
    _tool("freecad_new_document",
          "Create an empty FreeCAD document. A document is a .FCStd file in "
          "the output directory and persists between calls; every later "
          "FreeCAD tool addresses it by this name.",
          {"name": _STR}, ["name"]),
    _tool("freecad_add_solid",
          "Add a solid primitive to a FreeCAD document. Lengths in mm. "
          "kind is one of box (length,width,height), cylinder (radius,height), "
          "sphere (radius), cone (radius1,radius2,height), torus "
          "(radius1,radius2), tube (outer_radius,inner_radius,height).",
          {"document": _STR, "kind": _STR,
           "params": {"type": "object",
                      "description": "Dimensions in mm, keyed per kind."},
           "name": _STR, "position": _VEC3,
           "rotation": dict(_VEC3, description="XYZ Euler angles in degrees.")},
          ["document", "kind", "params"]),
    _tool("freecad_extrude_profile",
          "Extrude a closed 2D polygon along +Z into a solid. Points are "
          "[[x,y],...] in mm.",
          {"document": _STR,
           "points": {"type": "array", "items": {"type": "array", "items": _NUM}},
           "height": _NUM, "name": _STR, "position": _VEC3},
          ["document", "points", "height"]),
    _tool("freecad_revolve_profile",
          "Revolve a closed 2D polygon about an axis - nose cones, pressure "
          "vessels, turned parts. Points are [[x,z],...] in mm in the XZ plane.",
          {"document": _STR,
           "points": {"type": "array", "items": {"type": "array", "items": _NUM}},
           "angle": dict(_NUM, description="Degrees; 360 for a full revolve."),
           "axis": _VEC3, "name": _STR},
          ["document", "points"]),
    _tool("freecad_boolean",
          "Combine two objects in a FreeCAD document: cut, union or intersect.",
          {"document": _STR, "operation": {"type": "string",
                                           "enum": ["cut", "union", "intersect"]},
           "base": _STR, "tool": _STR, "name": _STR, "keep_originals": _BOOL},
          ["document", "operation", "base", "tool"]),
    _tool("freecad_fillet",
          "Round (or chamfer) the edges of an object. Without max_edge_length "
          "every edge is treated, which fails if the radius exceeds any edge.",
          {"document": _STR, "target": _STR, "radius": _NUM,
           "chamfer": _BOOL, "max_edge_length": _NUM},
          ["document", "target", "radius"]),
    _tool("freecad_measure",
          "Volume, surface area, bounding box, centre of mass and inertia of "
          "one object, in mm.",
          {"document": _STR, "target": _STR}, ["document", "target"]),
    _tool("freecad_export",
          "Export a FreeCAD document to STEP, IGES, BREP, STL, OBJ or PLY. "
          "Use STL to feed geometry into an OpenFOAM case.",
          {"document": _STR,
           "fmt": {"type": "string",
                   "enum": ["step", "iges", "brep", "stl", "obj", "ply"]},
           "path": _STR, "target": _STR,
           "linear_deflection": dict(_NUM, description=
               "Mesh formats only: max deviation in mm. 0.1 suits CFD and printing.")},
          ["document"]),
    _tool("freecad_import",
          "Import a STEP / IGES / BREP / STL file into a FreeCAD document.",
          {"document": _STR, "path": _STR, "name": _STR}, ["document", "path"]),
    _tool("freecad_document_info",
          "List every object in a FreeCAD document with its mass properties.",
          {"document": _STR}, ["document"]),
    _tool("freecad_run_script",
          "Run arbitrary FreeCAD Python headlessly. App, Part and the helpers "
          "_doc/_save/_obj/_shape_props are in scope; assign into the _result "
          "dict to return values. Use when no specific tool fits.",
          {"code": _STR, "document": _STR}, ["code"]),

    # --- Fusion ---
    _tool("fusion_install_addin",
          "Copy the SimKit add-in into Fusion's add-ins folder. Fusion must "
          "then be told to run it (Utilities > Add-Ins > SimKit > Run); there "
          "is no API to do that from outside.",
          {}),
    _tool("fusion_status",
          "Whether Fusion is running, has the add-in loaded, and is reachable.",
          {}),
    _tool("fusion_new_document", "Create a new parametric Fusion design.", {}),
    _tool("fusion_add_solid",
          "Add a solid to the active Fusion design. Lengths in mm. kind is "
          "box (length,width,height), cylinder (radius,height), tube "
          "(outer_radius,inner_radius,height) or polygon (points,height).",
          {"kind": _STR, "params": {"type": "object"}, "name": _STR,
           "position": _VEC3},
          ["kind", "params"]),
    _tool("fusion_boolean",
          "Cut, union or intersect two bodies in the active Fusion design.",
          {"operation": {"type": "string", "enum": ["cut", "union", "intersect"]},
           "base": _STR, "tool": _STR, "name": _STR, "keep_tool": _BOOL},
          ["operation", "base", "tool"]),
    _tool("fusion_fillet",
          "Round the edges of a body in Fusion. Radius in mm.",
          {"body": _STR, "radius": _NUM, "max_edge_length": _NUM},
          ["body", "radius"]),
    _tool("fusion_list_bodies",
          "List the solid bodies in the active Fusion design with volume and mass.",
          {}),
    _tool("fusion_parameters",
          "List the user parameters of the active Fusion design. These are the "
          "handles that drive a parametric model.",
          {}),
    _tool("fusion_set_parameter",
          "Change a Fusion user parameter and let the model rebuild. The "
          "expression is Fusion syntax, so '120 mm' or 'tube_od / 2' both work.",
          {"name": _STR, "expression": {"type": ["string", "number"]}},
          ["name", "expression"]),
    _tool("fusion_add_parameter",
          "Add a new user parameter to the active Fusion design.",
          {"name": _STR, "expression": {"type": ["string", "number"]},
           "units": _STR, "comment": _STR},
          ["name", "expression"]),
    _tool("fusion_export",
          "Export the Fusion design (or one body) to STEP, IGES, SAT, SMT, "
          "STL or F3D.",
          {"path": _STR,
           "fmt": {"type": "string",
                   "enum": ["step", "iges", "sat", "smt", "stl", "f3d"]},
           "body": _STR},
          ["path"]),
    _tool("fusion_screenshot",
          "Capture the Fusion viewport to a PNG, so the model can be checked "
          "by eye rather than only by numbers.",
          {"path": _STR, "width": {"type": "integer"},
           "height": {"type": "integer"}}),
    _tool("fusion_run_script",
          "Run arbitrary Fusion Python on Fusion's main thread. adsk, app, ui, "
          "design and root are in scope; assign into the result dict to return "
          "values. Use when no specific tool fits.",
          {"code": _STR}, ["code"]),

    # --- OpenRocket ---
    _tool("openrocket_examples",
          "With no name, list the example rocket designs bundled with "
          "OpenRocket. With a name, copy that example into the output "
          "directory to work on.",
          {"name": _STR, "save_as": _STR}),
    _tool("openrocket_design_info",
          "Read a .ork design: its component tree, stages and saved "
          "simulations. Does not simulate.",
          {"design": _STR}, ["design"]),
    _tool("openrocket_simulate",
          "Run one of a design's simulations using OpenRocket's own engine "
          "(Barrowman aerodynamics, RK4 trajectory, ISA atmosphere) and return "
          "apogee, max velocity, Mach, times and landing speed. The optional "
          "launch conditions override the saved ones for this run only.",
          {"design": _STR, "index": {"type": "integer"}, "series": _BOOL,
           "launch_rod_length": dict(_NUM, description="metres"),
           "launch_rod_angle_deg": _NUM,
           "wind_speed": dict(_NUM, description="m/s"),
           "launch_altitude": dict(_NUM, description="metres above sea level")},
          ["design"]),
    _tool("openrocket_simulate_all",
          "Run every simulation stored in a design and compare them side by "
          "side - the usual way to choose a motor.",
          {"design": _STR}, ["design"]),
    _tool("openrocket_parameters",
          "List every component of a .ork design with the numeric fields that "
          "can be changed. All values are SI: metres and radians.",
          {"design": _STR}, ["design"]),
    _tool("openrocket_set_parameters",
          "Change numeric fields on a named component of a design. Values are "
          "SI (metres, radians), matching the file itself.",
          {"design": _STR, "component": _STR,
           "changes": {"type": "object",
                       "description": "field -> new value, e.g. {\"length\": 0.3}"},
           "save_as": _STR},
          ["design", "component", "changes"]),
    _tool("openrocket_sweep",
          "Vary one component field across several values, simulating each, to "
          "show how a design choice affects the flight. Values are SI.",
          {"design": _STR, "component": _STR, "field": _STR,
           "values": {"type": "array", "items": _NUM},
           "simulate_each": _BOOL, "index": {"type": "integer"}},
          ["design", "component", "field", "values"]),

    # --- OpenFOAM ---
    _tool("openfoam_create_case",
          "Write a complete OpenFOAM case. Everything is SI: velocity in m/s, "
          "bounds in metres. Set geometry_stl to a file exported from FreeCAD "
          "or Fusion to put a real shape in the flow (stl_scale 0.001 converts "
          "the CAD millimetres to metres). Set force_coeffs true, with "
          "reference_area and reference_length, to measure drag and lift.",
          {"name": _STR,
           "velocity": dict(_VEC3, description="Free-stream velocity, m/s."),
           "bounds": {"type": "object",
                      "description": "{\"min\": [x,y,z], \"max\": [x,y,z]} in metres."},
           "cells": {"type": "array", "items": {"type": "integer"},
                     "description": "Background mesh divisions [nx, ny, nz]."},
           "nu": dict(_NUM, description="Kinematic viscosity m2/s; air is 1.5e-5."),
           "turbulence": {"type": "string",
                          "enum": ["laminar", "kEpsilon", "kOmegaSST",
                                   "realizableKE", "SpalartAllmaras"]},
           "two_d": _BOOL, "steady": _BOOL,
           "end_time": dict(_NUM, description=
               "Iterations when steady, seconds when transient."),
           "write_interval": _NUM,
           "reference_length": dict(_NUM, description=
               "Metres. Sets the Reynolds number and normalises the coefficients."),
           "geometry_stl": _STR, "surface_patch": _STR,
           "refinement": {"type": "integer"}, "stl_scale": _NUM,
           "force_coeffs": _BOOL, "reference_area": _NUM, "ground": _BOOL},
          ["name", "velocity", "bounds"]),
    _tool("openfoam_mesh",
          "Build the mesh for a case: blockMesh, then snappyHexMesh if the "
          "case has geometry, then checkMesh to confirm it is usable.",
          {"name": _STR, "check": _BOOL}, ["name"]),
    _tool("openfoam_solve",
          "Run the solver on a meshed case and report convergence: residuals, "
          "iterations, and whether anything is diverging.",
          {"name": _STR, "timeout": _NUM}, ["name"]),
    _tool("openfoam_force_coefficients",
          "Read the drag and lift coefficients from a solved case. Requires "
          "the case to have been created with force_coeffs true.",
          {"name": _STR, "average_last": {"type": "integer"}}, ["name"]),
    _tool("openfoam_status",
          "What exists on disk for a case: mesh, time directories, logs.",
          {"name": _STR}, ["name"]),
    _tool("openfoam_post",
          "Run a built-in OpenFOAM post-processing function on a case, such as "
          "'yPlus', 'mag(U)' or 'vorticity'.",
          {"name": _STR, "function": _STR, "time": _STR}, ["name", "function"]),
    _tool("openfoam_to_vtk",
          "Convert a solved case to VTK so it can be opened in ParaView.",
          {"name": _STR}, ["name"]),

    # --- Turbojet ---
    _tool("turbojet_cores",
          "List the turbocharger cores a homebuilt turbojet can be designed "
          "around, with wheel sizes and speed ratings. Start here.",
          {}),
    _tool("turbojet_design",
          "Close a turbojet design around a turbocharger core: thrust, "
          "pressure ratio, mass flow, fuel burn, the sizes of every part you "
          "must fabricate, and the constraint that is actually binding. With "
          "no rpm it searches for the most thrust the core can make inside its "
          "own speed rating. Physics is MechKit's; this adds the turbocharger "
          "constraint. Always prefer the measured_* arguments over a named "
          "core - the table entries are nominal and wheels vary between "
          "variants of the same turbo.",
          {"core": _STR,
           "rpm": dict(_NUM, description=
               "Operating speed. Omit to search for maximum thrust."),
           "turbine_inlet_temperature": dict(_NUM, description=
               "Kelvin. 1050 K suits stainless; higher needs Inconel."),
           "compressor_efficiency": dict(_NUM, description=
               "Default 0.72, deliberately below textbook: a turbo compressor "
               "was matched to a piston engine, not a jet."),
           "turbine_efficiency": _NUM,
           "measured_compressor_od_mm": dict(_NUM, description=
               "Measured compressor exducer (wide, back face) diameter."),
           "measured_inducer_mm": dict(_NUM, description=
               "Measured compressor inducer (inlet eye) diameter."),
           "measured_turbine_od_mm": _NUM,
           "measured_max_rpm": dict(_NUM, description=
               "Manufacturer speed rating for your variant.")}),
    _tool("turbojet_build",
          "Design the engine and then generate every part you fabricate - "
          "flame tube with its air-admission holes, casing, diffuser vanes, "
          "nozzle guide vanes and exhaust nozzle - as real CAD in a FreeCAD "
          "document, ready to export as STEP or STL.",
          {"core": _STR, "document": _STR, "rpm": _NUM,
           "turbine_inlet_temperature": _NUM,
           "measured_compressor_od_mm": _NUM, "measured_inducer_mm": _NUM,
           "measured_turbine_od_mm": _NUM, "measured_max_rpm": _NUM}),
    _tool("turbojet_flow_test",
          "Run compressible CFD on the exhaust nozzle and compare the mass "
          "flow it actually passes with what the 1-D cycle assumed. The nozzle "
          "exit area sets the engine's operating point, so a disagreement here "
          "matters: too much flow and the engine will not build pressure, too "
          "little and the turbine overheats. Solved on an axisymmetric wedge, "
          "which is both cheaper and more accurate than a coarse 3-D mesh. "
          "Takes a few minutes.",
          {"core": _STR, "name": _STR, "rpm": _NUM,
           "turbine_inlet_temperature": _NUM,
           "cells_axial": {"type": "integer"},
           "cells_radial": {"type": "integer"},
           "end_time": dict({"type": "integer"}, description=
               "Maximum SIMPLE iterations; it stops early once converged."),
           "timeout": _NUM,
           "measured_compressor_od_mm": _NUM, "measured_inducer_mm": _NUM,
           "measured_turbine_od_mm": _NUM, "measured_max_rpm": _NUM}),
    _tool("turbojet_size_nozzle",
          "Close the loop: repeatedly CFD the nozzle and scale its exit area "
          "until it actually passes the mass flow the cycle needs. The 1-D "
          "sizing assumes ideal expansion and is typically several percent "
          "out; this corrects it against the solved flow. Each round is a full "
          "solve, so allow several minutes.",
          {"core": _STR, "name": _STR, "rpm": _NUM,
           "turbine_inlet_temperature": _NUM,
           "tolerance": dict(_NUM, description=
               "Acceptable mass-flow error, as a fraction. 0.02 is 2%."),
           "max_rounds": {"type": "integer"},
           "end_time": {"type": "integer"}, "timeout": _NUM,
           "measured_compressor_od_mm": _NUM, "measured_inducer_mm": _NUM,
           "measured_turbine_od_mm": _NUM, "measured_max_rpm": _NUM}),

    # --- air-fed thruster ---
    _tool("thruster_design",
          "Size a small air-fed thruster: supply, chamber, nozzle, and nothing "
          "that spins. This is the right tool for a cheap, small, few-part RC "
          "thruster fed from a compressor or bottle - not the turbojet tools, "
          "which assume a turbocharger core and a self-sustaining cycle. Give "
          "a tank size to also get run time and thrust-to-weight.",
          {"supply_pressure_bar": dict(_NUM, description="Gauge pressure at the thruster."),
           "supply_cfm": dict(_NUM, description=
               "Compressor delivery in CFM of free air. Hobby units are 2-5, "
               "workshop units 10-20."),
           "chamber_temperature": dict(_NUM, description=
               "Kelvin. 288 for cold gas; higher only if you add a burner."),
           "divergent": dict(_BOOL, description=
               "Add a diverging cone. Buys little at modest supply pressure."),
           "tank_litres": dict(_NUM, description=
               "Bottle size, if running untethered. 0 to skip."),
           "tank_pressure_bar": _NUM,
           "airframe_mass_kg": _NUM}),
    _tool("thruster_supply_sweep",
          "Thrust against air supply. The single most useful table for this "
          "project: it shows that the compressor, not the nozzle, sets what "
          "the thruster can do.",
          {"supply_pressure_bar": _NUM, "chamber_temperature": _NUM,
           "divergent": _BOOL}),
    _tool("thruster_heat_benefit",
          "What adding a burner buys for a fixed air supply, and what it costs "
          "in materials and risk.",
          {"supply_cfm": _NUM, "supply_pressure_bar": _NUM}),
]

HANDLERS = {
    "survey": survey,
    "freecad_new_document": freecad_new_document,
    "freecad_add_solid": freecad_add_solid,
    "freecad_extrude_profile": freecad_extrude_profile,
    "freecad_revolve_profile": freecad_revolve_profile,
    "freecad_boolean": freecad_boolean,
    "freecad_fillet": freecad_fillet,
    "freecad_measure": freecad_measure,
    "freecad_export": freecad_export,
    "freecad_import": freecad_import,
    "freecad_document_info": freecad_document_info,
    "freecad_run_script": freecad_run_script,
    "fusion_install_addin": fusion_install_addin,
    "fusion_status": fusion_status,
    "fusion_new_document": fusion_new_document,
    "fusion_add_solid": fusion_add_solid,
    "fusion_boolean": fusion_boolean,
    "fusion_fillet": fusion_fillet,
    "fusion_list_bodies": fusion_list_bodies,
    "fusion_parameters": fusion_parameters,
    "fusion_set_parameter": fusion_set_parameter,
    "fusion_add_parameter": fusion_add_parameter,
    "fusion_export": fusion_export,
    "fusion_screenshot": fusion_screenshot,
    "fusion_run_script": fusion_run_script,
    "openrocket_examples": openrocket_examples,
    "openrocket_design_info": openrocket_design_info,
    "openrocket_simulate": openrocket_simulate,
    "openrocket_simulate_all": openrocket_simulate_all,
    "openrocket_parameters": openrocket_parameters,
    "openrocket_set_parameters": openrocket_set_parameters,
    "openrocket_sweep": openrocket_sweep,
    "openfoam_create_case": openfoam_create_case,
    "openfoam_mesh": openfoam_mesh,
    "openfoam_solve": openfoam_solve,
    "openfoam_force_coefficients": openfoam_force_coefficients,
    "openfoam_status": openfoam_status,
    "openfoam_post": openfoam_post,
    "openfoam_to_vtk": openfoam_to_vtk,
    "turbojet_cores": turbojet_cores,
    "turbojet_design": turbojet_design,
    "turbojet_build": turbojet_build,
    "turbojet_flow_test": turbojet_flow_test,
    "turbojet_size_nozzle": turbojet_size_nozzle,
    "thruster_design": thruster_design,
    "thruster_supply_sweep": thruster_supply_sweep,
    "thruster_heat_benefit": thruster_heat_benefit,
}

assert {t["name"] for t in TOOLS} == set(HANDLERS), (
    "TOOLS and HANDLERS disagree: "
    f"{ {t['name'] for t in TOOLS} ^ set(HANDLERS) }"
)


def dispatch(name: str, arguments: dict) -> str:
    """Run one tool. Failures come back as readable text, not tracebacks.

    An application that is not installed, not running, or given impossible
    geometry is an ordinary outcome here -- the caller needs to read what went
    wrong and choose differently, which it cannot do from a stack trace.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool {name!r}. Available: {', '.join(sorted(HANDLERS))}"
    try:
        return handler(**{k: v for k, v in (arguments or {}).items() if v is not None})
    except TypeError as exc:
        return f"{name}: bad arguments - {exc}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if os.environ.get("SIMKIT_DEBUG"):
            detail += "\n" + traceback.format_exc()
        return f"{name} failed - {detail}"
