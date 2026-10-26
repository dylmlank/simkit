"""Meshing, solving and post-processing an OpenFOAM case.

Every function here reports what OpenFOAM actually said. Solver logs are long
and mostly noise, so the useful signal -- did the mesh pass its checks, are the
residuals still falling, what is the drag coefficient doing -- is parsed out and
returned as numbers. Nothing is smoothed over: a diverged run is reported as
diverged.
"""
from __future__ import annotations

import os
import re

from . import wsl

#: "smoothSolver:  Solving for Ux, Initial residual = 0.00135, Final residual = ..."
RESIDUAL = re.compile(
    r"Solving for (\w+),\s+Initial residual = ([0-9.eE+-]+),\s+"
    r"Final residual = ([0-9.eE+-]+)")
TIME_LINE = re.compile(r"^Time = ([0-9.eE+-]+)", re.M)
COURANT = re.compile(r"Courant Number mean: ([0-9.eE+-]+) max: ([0-9.eE+-]+)")
#: blockMesh prints "  nCells: 1800", checkMesh "    cells:   1800".
CELLS = re.compile(r"^\s*n?[Cc]ells:\s+(\d+)", re.M)
#: snappyHexMesh reports "Snapped mesh : cells:38298  faces:...  points:..."
SNAPPY_CELLS = re.compile(r"\bcells:(\d+)")


def _case(name: str) -> str:
    from . import case as case_mod
    path = case_mod.case_dir(name)
    if not os.path.isdir(path):
        raise FileNotFoundError(
            f"no case at {path}. Create one first with openfoam_create_case.")
    return path


def block_mesh(name: str) -> dict:
    """Generate the background mesh from system/blockMeshDict."""
    path = _case(name)
    ran = wsl.run("blockMesh", path, log_name="log.blockMesh").require()
    cells = CELLS.search(ran.stdout)
    return {
        "case": path,
        "step": "blockMesh",
        "cells": int(cells.group(1)) if cells else None,
        "log": ran.log_path,
        "tail": ran.tail(6),
    }


def snappy(name: str, parallel: int = 0) -> dict:
    """Cut the STL geometry into the background mesh.

    `-overwrite` keeps the result in constant/polyMesh rather than writing a
    time directory per stage, which is what the solver expects to find.
    """
    path = _case(name)
    if not os.path.isfile(os.path.join(path, "system", "snappyHexMeshDict")):
        raise FileNotFoundError(
            "this case has no snappyHexMeshDict -- it was created without "
            "geometry_stl, so there is nothing to snap.")
    cmd = "snappyHexMesh -overwrite"
    if parallel > 1:
        cmd = (f"decomposePar -force && mpirun -np {int(parallel)} "
               f"snappyHexMesh -overwrite -parallel && reconstructParMesh -constant")
    ran = wsl.run(cmd, path, log_name="log.snappyHexMesh").require()
    cells = SNAPPY_CELLS.findall(ran.stdout) or CELLS.findall(ran.stdout)
    return {
        "case": path,
        "step": "snappyHexMesh",
        "cells": int(cells[-1]) if cells else None,
        "log": ran.log_path,
        "tail": ran.tail(8),
    }


def check_mesh(name: str) -> dict:
    """Run checkMesh and say plainly whether the mesh is usable."""
    path = _case(name)
    ran = wsl.run("checkMesh", path, log_name="log.checkMesh")
    text = ran.stdout
    failed = [line.strip() for line in text.splitlines() if line.strip().startswith("***")]
    cells = CELLS.search(text)
    return {
        "case": path,
        "step": "checkMesh",
        "mesh_ok": "Mesh OK." in text,
        "cells": int(cells.group(1)) if cells else None,
        "failures": failed,
        "log": ran.log_path,
    }


def _case_settings(path: str) -> tuple[bool, str, bool]:
    """Recover how this case was written: steady/transient, fork, and regime."""
    steady, flavor, compressible = True, "", False
    marker = os.path.join(path, ".simkit")
    if os.path.isfile(marker):
        for line in open(marker, encoding="utf-8"):
            key, _, value = line.strip().partition("=")
            if key == "steady":
                steady = value == "1"
            elif key == "flavor":
                flavor = value
            elif key == "compressible":
                compressible = value == "1"
    return steady, flavor, compressible


def solve(name: str, timeout: float | None = None) -> dict:
    """Run the solver this case was written for."""
    from .. import paths

    path = _case(name)
    if not os.path.isdir(os.path.join(path, "constant", "polyMesh")):
        raise FileNotFoundError(
            "this case has no mesh yet. Run openfoam_mesh before solving.")

    steady, flavor, compressible = _case_settings(path)
    dia = paths.openfoam_dialect()
    if flavor and flavor != dia.flavor:
        raise RuntimeError(
            f"this case was written for the {flavor} build of OpenFOAM but the "
            f"active install is {dia.flavor} ({dia.describe()}). The two forks "
            "use different dictionary names. Recreate the case, or point "
            "SIMKIT_OPENFOAM_BASHRC at the matching install."
        )

    command = dia.run_command(steady, compressible=compressible)
    ran = wsl.run(command, path, timeout=timeout, log_name=f"log.{command}")
    summary = parse_log(ran.stdout)
    summary.update({
        "case": path,
        "step": command,
        "mode": ("steady" if steady else "transient")
                + (", compressible" if compressible else ""),
        "completed": ran.ok and "End" in ran.stdout,
        "log": ran.log_path,
    })
    if not ran.ok:
        summary["failed"] = True
        summary["error_tail"] = ran.tail(20)
    return summary


def parse_log(text: str) -> dict:
    """Pull the convergence story out of a solver log."""
    times = TIME_LINE.findall(text)
    initial: dict[str, list[float]] = {}
    for field, first, _final in RESIDUAL.findall(text):
        try:
            initial.setdefault(field, []).append(float(first))
        except ValueError:
            continue

    # The initial residual of the last outer iteration is the honest measure of
    # convergence: how wrong the solution still was when the step began.
    final_residuals = {f: v[-1] for f, v in initial.items() if v}
    worst = max(final_residuals.values()) if final_residuals else None
    courant = COURANT.findall(text)

    diverging = [f for f, v in initial.items()
                 if len(v) > 10 and v[-1] > max(v[0], 1e-6) * 10]

    # A steady run says so explicitly when its residual controls are met; that
    # statement is worth more than any threshold guessed at from outside.
    declared = re.search(r"solution converged in (\d+) iterations", text)

    # How far each field actually fell, which is the meaningful question --
    # the absolute value alone cannot distinguish "converged" from "never had
    # anything to solve".
    drops = {f: v[0] / v[-1] for f, v in initial.items() if len(v) > 1 and v[-1] > 0}
    best_drop = max(drops.values()) if drops else 0.0

    note = ""
    if (worst is not None and worst > 1e-3 and best_drop < 10
            and not diverging and len(times) > 5):
        # OpenFOAM normalises residuals by the variation in the field. On a
        # uniform solution that denominator collapses to round-off and the
        # ratio stays O(1) no matter how exact the answer is. Flat-from-the-
        # start residuals mean this, not failure.
        note = ("residuals were flat from the first iteration: the initial "
                "field already satisfies the equations (normal for an empty "
                "domain). Judge this run by its field values, not its residuals.")

    return {
        "time_steps": len(times),
        "last_time": float(times[-1]) if times else None,
        "final_initial_residuals": {f: float(f"{v:.3e}")
                                    for f, v in final_residuals.items()},
        "worst_residual": float(f"{worst:.3e}") if worst is not None else None,
        "residual_drop_factor": round(best_drop, 1) if best_drop else None,
        "converged": bool(declared) or bool(worst is not None and worst < 1e-3),
        "converged_in_iterations": int(declared.group(1)) if declared else None,
        "diverging_fields": diverging,
        "max_courant": float(courant[-1][1]) if courant else None,
        **({"note": note} if note else {}),
    }


def force_coefficients(name: str, last_n: int = 1) -> dict:
    """Read drag/lift coefficients written by the forceCoeffs function object.

    Column names come from the file's own header rather than a fixed order,
    because that layout has changed between OpenFOAM releases.
    """
    path = _case(name)
    root = os.path.join(path, "postProcessing", "forceCoeffs")
    if not os.path.isdir(root):
        raise FileNotFoundError(
            "no force coefficients were written. The case has to be created "
            "with force_coeffs=True and then solved.")

    dat = ""
    for time_dir in sorted(os.listdir(root), key=_as_float):
        for candidate in ("coefficient.dat", "forceCoeffs.dat", "coefficient_0.dat"):
            p = os.path.join(root, time_dir, candidate)
            if os.path.isfile(p):
                dat = p
    if not dat:
        raise FileNotFoundError(f"no coefficient data file under {root}")

    columns: list[str] = []
    rows: list[list[float]] = []
    with open(dat, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#"):
                stripped = line.lstrip("#").strip()
                # The last comment line before the data is the column header.
                if stripped.split()[:1] == ["Time"]:
                    columns = stripped.split()
                continue
            if not line:
                continue
            try:
                rows.append([float(v) for v in line.split()])
            except ValueError:
                continue

    if not rows:
        raise ValueError(f"{dat} contains no numeric rows yet")
    if not columns or len(columns) != len(rows[0]):
        columns = ["Time"] + [f"c{i}" for i in range(1, len(rows[0]))]

    latest = dict(zip(columns, rows[-1]))
    window = rows[-max(1, int(last_n)):]
    averaged = {
        col: sum(r[i] for r in window) / len(window)
        for i, col in enumerate(columns) if col != "Time"
    }
    return {
        "case": path,
        "file": dat,
        "samples": len(rows),
        "time": latest.get("Time"),
        "latest": {k: round(v, 6) for k, v in latest.items() if k != "Time"},
        "mean_last_n": {k: round(v, 6) for k, v in averaged.items()},
        "n_averaged": len(window),
    }


def _group_vectors(tokens: list) -> list:
    """Rejoin `(x y z)` triples that whitespace splitting tore apart.

    A surfaceFieldValue over U writes its value as a parenthesised vector, so a
    naive split leaves "(1.6e+02" as one column and shifts every column after
    it. That misalignment is silent and turns pressure into velocity.
    """
    out, buffer = [], None
    for token in tokens:
        if buffer is not None:
            buffer.append(token)
            if token.endswith(")"):
                out.append(" ".join(buffer))
                buffer = None
        elif token.startswith("(") and not token.endswith(")"):
            buffer = [token]
        else:
            out.append(token)
    if buffer:
        out.append(" ".join(buffer))
    return out


def _last_data_row(path: str) -> tuple[list, list] | None:
    """Column names and the final row of an OpenFOAM function-object .dat."""
    columns, last = [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#"):
                stripped = line.lstrip("#").strip()
                if stripped.split()[:1] == ["Time"]:
                    columns = stripped.split()
                continue
            if line:
                last = _group_vectors(line.split())
    if last is None:
        return None
    return columns, last


def flow_summary(name: str) -> dict:
    """Mass flow and mean conditions at each measured patch.

    `phi` on a patch is the flux summed over its faces: negative at an inlet
    because the face normals point out of the domain. The magnitude is what
    matters, and for a compressible solver it is already a mass flow in kg/s
    rather than a volumetric one.
    """
    path = _case(name)
    root = os.path.join(path, "postProcessing")
    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"no postProcessing directory in {path}. Either the case has not "
            "been solved, or it was created without flow function objects.")

    out: dict = {"case": path, "patches": {}}
    for entry in sorted(os.listdir(root)):
        kind, _, patch = entry.partition("_")
        if kind not in ("massFlow", "total") or not patch:
            continue
        # Take the newest time directory that actually holds data.
        newest = None
        for time_dir in sorted(os.listdir(os.path.join(root, entry)),
                               key=_as_float):
            folder = os.path.join(root, entry, time_dir)
            if not os.path.isdir(folder):
                continue
            for filename in os.listdir(folder):
                if filename.endswith(".dat"):
                    newest = os.path.join(folder, filename)
        if newest is None:
            continue
        parsed = _last_data_row(newest)
        if parsed is None:
            continue
        columns, row = parsed
        record = out["patches"].setdefault(patch, {})
        if kind == "massFlow":
            try:
                record["mass_flow_kg_s"] = round(abs(float(row[1])), 6)
            except (IndexError, ValueError):
                pass
        else:
            for i, column in enumerate(columns[1:], start=1):
                if i < len(row):
                    try:
                        record[column] = round(float(row[i]), 4)
                    except ValueError:
                        record[column] = row[i]
        record["time"] = _as_float(row[0]) if row else None

    if not out["patches"]:
        raise ValueError(
            f"no mass-flow data found under {root}. The solver may have "
            "stopped before writing any.")
    return out


def _as_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return -1.0


def status(name: str) -> dict:
    """What exists on disk for this case: mesh, time directories, logs."""
    path = _case(name)
    times = sorted(
        (d for d in os.listdir(path)
         if _as_float(d) >= 0 and os.path.isdir(os.path.join(path, d))),
        key=_as_float,
    )
    logs = sorted(f for f in os.listdir(path) if f.startswith("log."))
    return {
        "case": path,
        "meshed": os.path.isdir(os.path.join(path, "constant", "polyMesh")),
        "time_directories": times,
        "latest_time": times[-1] if times else None,
        "logs": logs,
        "has_force_coeffs": os.path.isdir(
            os.path.join(path, "postProcessing", "forceCoeffs")),
    }


def post_process(name: str, function: str, time: str = "") -> dict:
    """Run a built-in postProcess function, e.g. `mag(U)` or `yPlus`."""
    from .. import paths

    path = _case(name)
    command = paths.openfoam_dialect().post_process_command
    arg = f"-func {wsl.shquote(function)}"
    if time:
        arg += f" -time {wsl.shquote(time)}"
    ran = wsl.run(f"{command} {arg}", path,
                  log_name="log.postProcess").require()
    return {"case": path, "function": function, "command": command,
            "tail": ran.tail(15), "log": ran.log_path}


def sample_line(name: str, start, end, fields=("U", "p", "T"),
                points: int = 200, label: str = "line") -> dict:
    """Sample fields along a straight line through the solved domain.

    This is how you see what the flow is actually doing rather than what the
    integrated numbers imply -- where a nozzle accelerates, where it separates,
    whether the pressure drop happens at the throat or earlier.

    Coordinates are metres, in the case's own frame.
    """
    from .. import paths

    path = _case(name)
    dia = paths.openfoam_dialect()
    field_list = " ".join(fields)

    body = f"""
{label}
{{
    type            sets;
    libs            ("libsampling.so");
    writeControl    onEnd;
    setFormat       raw;
    interpolationScheme cellPoint;
    fields          ({field_list});

    sets
    {{
        {label}
        {{
            // `uniform`, not `lineUniform` -- both forks use the short name,
            // and the wrong one fails with a list of valid types rather than
            // anything that names the mistake.
            type            uniform;
            axis            distance;
            start           ({start[0]} {start[1]} {start[2]});
            end             ({end[0]} {end[1]} {end[2]});
            nPoints         {int(points)};
        }}
    }}
}}
"""
    from .case import _write
    _write(path, "system", label, "dictionary", body)

    ran = wsl.run(f"{dia.post_process_command} -func {label} -latestTime",
                  path, log_name=f"log.sample_{label}")
    if not ran.ok:
        raise RuntimeError(
            f"sampling failed: {ran.tail(12)}\n"
            "  A line that leaves the mesh is the usual cause. On a wedge the "
            "sample must sit just off the axis, inside the sector.")

    root = os.path.join(path, "postProcessing", label)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"no sampled output under {root}")

    newest, data_files = None, []
    for time_dir in sorted(os.listdir(root), key=_as_float):
        folder = os.path.join(root, time_dir)
        if os.path.isdir(folder):
            newest = folder
    if newest:
        data_files = sorted(os.listdir(newest))

    columns: dict[str, list] = {"distance": []}
    for filename in data_files:
        # raw format writes one file per field group: <set>_<fields>.xy
        with open(os.path.join(newest, filename), encoding="utf-8",
                  errors="replace") as fh:
            rows = [line.split() for line in fh
                    if line.strip() and not line.startswith("#")]
        if not rows:
            continue
        names = _names_from(filename, len(rows[0]) - 1)
        if not columns["distance"]:
            columns["distance"] = [float(r[0]) for r in rows]
        for i, column in enumerate(names, start=1):
            columns.setdefault(column, [])
            columns[column] = [float(r[i]) if i < len(r) else None for r in rows]

    return {"case": path, "label": label, "points": len(columns["distance"]),
            "data": columns, "directory": newest}


def _names_from(filename: str, count: int) -> list:
    """Field names from a raw sample filename, e.g. `line_p_T_U.xy`."""
    stem = os.path.splitext(filename)[0]
    parts = stem.split("_")[1:]
    names = []
    for part in parts:
        if part == "U":
            names.extend(["Ux", "Uy", "Uz"])
        else:
            names.append(part)
    if len(names) != count:
        names = [f"c{i}" for i in range(count)]
    return names


def to_vtk(name: str) -> dict:
    """Convert results to VTK so they can be opened in ParaView on Windows."""
    path = _case(name)
    ran = wsl.run("foamToVTK", path, log_name="log.foamToVTK").require()
    vtk_dir = os.path.join(path, "VTK")
    return {"case": path, "vtk": vtk_dir if os.path.isdir(vtk_dir) else "",
            "tail": ran.tail(5), "log": ran.log_path}
