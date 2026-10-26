"""Writing an OpenFOAM case directory.

An OpenFOAM case is a directory of dictionaries, not an API, so this module's
job is to emit correct text:

    <case>/system/    controlDict, fvSchemes, fvSolution, blockMeshDict
    <case>/constant/  physicalProperties, momentumTransport
    <case>/0/         the initial and boundary conditions for every field

The dictionary names above are OpenFOAM 13 (the Foundation line). They matter:
v11 renamed `transportProperties` to `physicalProperties` and
`turbulenceProperties` to `momentumTransport`, and replaced the per-solver
executables with a single `foamRun` that reads `solver` out of controlDict. A
case written for `simpleFoam` will not run here.

Boundary conditions are derived from a patch's *role* rather than spelled out
field by field. Saying a patch is an inlet fixes the velocity, lets pressure
float, and sets the turbulence quantities from the inlet velocity and length
scale -- which is both less to get wrong and how the physics actually works.
"""
from __future__ import annotations

import math
import os
import shutil

import config
from .. import paths

HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
  =========                 |
  \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\\\    /   O peration     | Written by SimKit
    \\\\  /    A nd           |
     \\\\/     M anipulation  |
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    format      ascii;
    class       {cls};
    location    "{location}";
    object      {obj};
}}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

"""

FOOTER = "\n// ************************************************************************* //\n"

#: The turbulence models SimKit knows how to write fields for, and the extra
#: transported quantities each one needs beyond U and p.
TURBULENCE_FIELDS = {
    "laminar": [],
    "kEpsilon": ["k", "epsilon", "nut"],
    "realizableKE": ["k", "epsilon", "nut"],
    "kOmegaSST": ["k", "omega", "nut"],
    "SpalartAllmaras": ["nuTilda", "nut"],
}

#: Wall treatment per field. A wall function models the near-wall layer instead
#: of resolving it, which is what makes a coarse mesh give sane drag.
WALL_FUNCTIONS = {
    "k": "kqRWallFunction",
    "epsilon": "epsilonWallFunction",
    "omega": "omegaWallFunction",
    "nut": "nutkWallFunction",
    "nuTilda": "fixedValue",
}

C_MU = 0.09


class Patch:
    """One boundary of the domain.

    `role` is the physical intent -- inlet, outlet, wall, slip, empty -- and
    every field's boundary condition follows from it.
    """

    ROLES = ("inlet", "outlet", "wall", "slip", "symmetry", "empty")

    def __init__(self, name: str, role: str, foam_type: str = "", faces=None):
        role = role.lower()
        if role not in self.ROLES:
            raise ValueError(
                f"unknown patch role {role!r}. Use one of: {', '.join(self.ROLES)}")
        self.name = name
        self.role = role
        self.faces = faces or []
        self.foam_type = foam_type or {
            "wall": "wall", "empty": "empty", "symmetry": "symmetry",
        }.get(role, "patch")


def _fmt(value) -> str:
    """Numbers the way OpenFOAM likes them: no numpy repr, no stray precision."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def _vec(v) -> str:
    return "(" + " ".join(_fmt(float(x)) for x in v) + ")"


def _write(case_dir: str, location: str, obj: str, cls: str, body: str) -> str:
    path = os.path.join(case_dir, location, obj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HEADER.format(cls=cls, location=location, obj=obj))
        fh.write(body.strip() + "\n")
        fh.write(FOOTER)
    return path


# ---------------------------------------------------------------------------
# Inlet turbulence
# ---------------------------------------------------------------------------

def inlet_turbulence(speed: float, length: float, intensity: float = 0.05) -> dict:
    """Turbulent inlet quantities from speed, a length scale and an intensity.

    These are the standard engineering estimates, not fitted constants: the
    mixing length is taken as 7% of the reference length, and k, epsilon and
    omega follow from their definitions. Given no better information about the
    incoming flow, this is what a CFD engineer would type.
    """
    speed = abs(float(speed))
    if speed == 0:
        return {"k": 1e-8, "epsilon": 1e-8, "omega": 1e-8, "nuTilda": 0.0}
    mixing_length = max(0.07 * float(length), 1e-6)
    k = 1.5 * (speed * intensity) ** 2
    epsilon = (C_MU ** 0.75) * (k ** 1.5) / mixing_length
    omega = (k ** 0.5) / ((C_MU ** 0.25) * mixing_length)
    return {"k": k, "epsilon": epsilon, "omega": omega, "nuTilda": 5 * 1e-5}


# ---------------------------------------------------------------------------
# Boundary conditions per field, derived from patch role
# ---------------------------------------------------------------------------

def _bc(field: str, role: str, values: dict) -> str:
    """The boundary condition block for one field on one patch role."""
    if role == "empty":
        return "        type            empty;"
    if role in ("slip", "symmetry"):
        return f"        type            {role};"

    inlet_value = values.get(field)

    if field == "U":
        if role == "inlet":
            return ("        type            fixedValue;\n"
                    f"        value           uniform {_vec(values['velocity'])};")
        if role == "outlet":
            # inletOutlet, not zeroGradient: if the solution transiently pushes
            # flow back in through the outlet, this keeps it bounded instead of
            # letting the run blow up.
            return ("        type            inletOutlet;\n"
                    "        inletValue      uniform (0 0 0);\n"
                    f"        value           uniform {_vec(values['velocity'])};")
        return "        type            noSlip;"

    if field == "p":
        if role == "outlet":
            return ("        type            fixedValue;\n"
                    "        value           uniform 0;")
        return "        type            zeroGradient;"

    if field == "nut":
        if role == "wall":
            return ("        type            nutkWallFunction;\n"
                    "        value           uniform 0;")
        return ("        type            calculated;\n"
                "        value           uniform 0;")

    # k, epsilon, omega, nuTilda
    if role == "inlet":
        return ("        type            fixedValue;\n"
                f"        value           uniform {_fmt(inlet_value)};")
    if role == "outlet":
        return ("        type            inletOutlet;\n"
                f"        inletValue      uniform {_fmt(inlet_value)};\n"
                f"        value           uniform {_fmt(inlet_value)};")
    wall_fn = WALL_FUNCTIONS.get(field, "zeroGradient")
    if wall_fn == "fixedValue":
        return ("        type            fixedValue;\n"
                "        value           uniform 0;")
    return (f"        type            {wall_fn};\n"
            f"        value           uniform {_fmt(inlet_value)};")


FIELD_META = {
    "U": ("volVectorField", "[0 1 -1 0 0 0 0]"),
    "p": ("volScalarField", "[0 2 -2 0 0 0 0]"),
    "k": ("volScalarField", "[0 2 -2 0 0 0 0]"),
    "epsilon": ("volScalarField", "[0 2 -3 0 0 0 0]"),
    "omega": ("volScalarField", "[0 0 -1 0 0 0 0]"),
    "nut": ("volScalarField", "[0 2 -1 0 0 0 0]"),
    "nuTilda": ("volScalarField", "[0 2 -1 0 0 0 0]"),
}


def write_field(case_dir: str, field: str, patches: list, values: dict) -> str:
    cls, dims = FIELD_META[field]
    if field == "U":
        internal = f"uniform {_vec(values['velocity'])}"
    elif field in ("p", "nut"):
        internal = "uniform 0"
    else:
        internal = f"uniform {_fmt(values.get(field, 0))}"

    blocks = []
    for p in patches:
        blocks.append(f"    {p.name}\n    {{\n{_bc(field, p.role, values)}\n    }}")
    body = (
        f"dimensions      {dims};\n\n"
        f"internalField   {internal};\n\n"
        "boundaryField\n{\n" + "\n\n".join(blocks) + "\n}"
    )
    return _write(case_dir, "0", field, cls, body)


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------

#: Face vertex orderings for a hex block, with normals pointing out of the
#: domain -- OpenFOAM rejects the mesh if any of these is reversed.
BOX_FACES = {
    "x-min": (0, 4, 7, 3),
    "x-max": (2, 6, 5, 1),
    "y-min": (1, 5, 4, 0),
    "y-max": (3, 7, 6, 2),
    "z-min": (0, 3, 2, 1),
    "z-max": (4, 5, 6, 7),
}


def box_patches(two_d: bool, ground: bool = False) -> list:
    """The six sides of a box domain, named and given roles.

    In 2D the two z faces become a single `empty` patch, which is how OpenFOAM
    is told to solve a one-cell-thick mesh as a plane problem.
    """
    if two_d:
        return [
            Patch("inlet", "inlet", faces=[BOX_FACES["x-min"]]),
            Patch("outlet", "outlet", faces=[BOX_FACES["x-max"]]),
            Patch("lowerWall", "wall" if ground else "slip",
                  foam_type="wall" if ground else "patch",
                  faces=[BOX_FACES["y-min"]]),
            Patch("upperWall", "slip", faces=[BOX_FACES["y-max"]]),
            Patch("frontAndBack", "empty",
                  faces=[BOX_FACES["z-min"], BOX_FACES["z-max"]]),
        ]
    return [
        Patch("inlet", "inlet", faces=[BOX_FACES["x-min"]]),
        Patch("outlet", "outlet", faces=[BOX_FACES["x-max"]]),
        Patch("side1", "slip", faces=[BOX_FACES["y-min"]]),
        Patch("side2", "slip", faces=[BOX_FACES["y-max"]]),
        Patch("ground", "wall" if ground else "slip",
              foam_type="wall" if ground else "patch",
              faces=[BOX_FACES["z-min"]]),
        Patch("top", "slip", faces=[BOX_FACES["z-max"]]),
    ]


def write_block_mesh(case_dir: str, bounds: dict, cells, patches: list,
                     dia=None) -> str:
    """A single graded hex block spanning `bounds`."""
    dia = dia or paths.openfoam_dialect()
    lo = [float(v) for v in bounds["min"]]
    hi = [float(v) for v in bounds["max"]]
    for i, axis in enumerate("xyz"):
        if hi[i] <= lo[i]:
            raise ValueError(
                f"domain {axis}-max ({hi[i]}) must be greater than {axis}-min ({lo[i]})")
    nx, ny, nz = (max(1, int(c)) for c in cells)

    verts = [
        (lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]),
        (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
        (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]),
        (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2]),
    ]
    vert_text = "\n".join(f"    {_vec(v)}" for v in verts)

    bnd = []
    for p in patches:
        faces = "\n".join(f"            ({' '.join(str(i) for i in f)})"
                          for f in p.faces)
        bnd.append(
            f"    {p.name}\n    {{\n        type            {p.foam_type};\n"
            f"        faces\n        (\n{faces}\n        );\n    }}")

    body = (
        f"{dia.scale_key} 1;\n\n"
        f"vertices\n(\n{vert_text}\n);\n\n"
        "blocks\n(\n"
        f"    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)\n"
        ");\n\n"
        "edges\n(\n);\n\n"
        f"boundary\n(\n" + "\n\n".join(bnd) + "\n);\n\n"
        "mergePatchPairs\n(\n);"
    )
    return _write(case_dir, "system", "blockMeshDict", "dictionary", body)


def write_snappy(case_dir: str, stl_name: str, surface_patch: str,
                 inside_point, refinement: int = 2, layers: int = 0,
                 stl_scale: float = 0.001, dia=None) -> str:
    """Cut a watertight STL surface out of the background mesh.

    This is the bridge from CAD to CFD: a solid exported from FreeCAD or Fusion
    becomes a wall in the flow domain. `inside_point` must be a location in the
    fluid -- not inside the solid -- because that is how snappyHexMesh decides
    which side of the surface to keep.

    `stl_scale` defaults to 0.001 because both CAD connectors export
    millimetres while OpenFOAM works in metres. Left unscaled, a 50 mm sphere
    would arrive as a 50 m one and swallow the entire domain -- a mistake that
    produces a plausible-looking mesh and silently meaningless drag.
    """
    dia = dia or paths.openfoam_dialect()
    lvl = max(0, int(refinement))
    body = f"""
castellatedMesh true;
snap            true;
addLayers       {'true' if layers > 0 else 'false'};

geometry
{{
    {surface_patch}
    {{
        type triSurfaceMesh;
        file "{stl_name}";
        scale {_fmt(float(stl_scale))};
    }}
}}

castellatedMeshControls
{{
    maxLocalCells       1000000;
    maxGlobalCells      4000000;
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels 3;
    resolveFeatureAngle 30;
    allowFreeStandingZoneFaces true;

    features ();

    refinementSurfaces
    {{
        {surface_patch}
        {{
            level ({lvl} {lvl + 1});
            patchInfo {{ type wall; }}
        }}
    }}

    refinementRegions {{}}

    {dia.inside_point_key} {_vec(inside_point)};
}}

snapControls
{{
    nSmoothPatch    3;
    tolerance       2.0;
    nSolveIter      30;
    nRelaxIter      5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes   true;
    layers
    {{
        "{surface_patch}.*"
        {{
            nSurfaceLayers {max(0, int(layers))};
        }}
    }}
    expansionRatio      1.2;
    finalLayerThickness 0.3;
    minThickness        0.1;
    nGrow               0;
    featureAngle        60;
    nRelaxIter          3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals      3;
    nSmoothThickness    10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle  90;
    nBufferCellsNoExtrude 0;
    nLayerIter          50;
}}

meshQualityControls
{{
    maxNonOrtho         65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave          80;
    minVol              1e-13;
    minTetQuality       1e-15;
    minArea             -1;
    minTwist            0.02;
    minDeterminant      0.001;
    minFaceWeight       0.02;
    minVolRatio         0.01;
    minTriangleTwist    -1;
    nSmoothScale        4;
    errorReduction      0.75;
}}

writeFlags ( scalarLevels layerSets layerFields );
mergeTolerance 1e-6;
"""
    return _write(case_dir, "system", "snappyHexMeshDict", "dictionary", body)


# ---------------------------------------------------------------------------
# system/ and constant/
# ---------------------------------------------------------------------------

def write_control(case_dir: str, end_time: float, write_interval: float,
                  delta_t: float = 0.001, max_co: float = 5.0,
                  functions: str = "", steady: bool = False, dia=None) -> str:
    """controlDict.

    In a steady run the "time" axis is iteration count, so the step is fixed at
    1 and the adaptive-timestep machinery is left off; in a transient run it is
    seconds and the step is chosen to respect the Courant limit.
    """
    dia = dia or paths.openfoam_dialect()
    fn_block = f"\nfunctions\n{{\n{functions}\n}}\n" if functions else ""

    if steady:
        timing = (
            f"endTime         {_fmt(int(end_time))};\n"
            "deltaT          1;\n\n"
            "writeControl    timeStep;\n"
            f"writeInterval   {_fmt(int(write_interval))};"
        )
    else:
        timing = (
            f"endTime         {_fmt(float(end_time))};\n"
            f"deltaT          {_fmt(float(delta_t))};\n\n"
            "writeControl    adjustableRunTime;\n"
            f"writeInterval   {_fmt(float(write_interval))};\n\n"
            "adjustTimeStep  yes;\n"
            f"maxCo           {_fmt(float(max_co))};"
        )

    body = f"""
{dia.control_solver_key}          {dia.solver_name(steady)};

startFrom       latestTime;
startTime       0;
stopAt          endTime;
{timing}

purgeWrite      0;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable yes;
{fn_block}"""
    return _write(case_dir, "system", "controlDict", "dictionary", body)


def write_schemes(case_dir: str, steady: bool = False) -> str:
    body = f"""
ddtSchemes
{{
    default         {"steadyState" if steady else "Euler"};
}}

gradSchemes
{{
    default         Gauss linear;
}}

divSchemes
{{
    default             none;
    div(phi,U)          Gauss linearUpwind grad(U);
    div(phi,k)          Gauss upwind;
    div(phi,epsilon)    Gauss upwind;
    div(phi,omega)      Gauss upwind;
    div(phi,nuTilda)    Gauss upwind;
    div(phi,R)          Gauss upwind;
    div(R)              Gauss linear;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}}

laplacianSchemes
{{
    default         Gauss linear corrected;
}}

interpolationSchemes
{{
    default         linear;
}}

snGradSchemes
{{
    default         corrected;
}}

// Required by any model that needs distance to the nearest wall --
// kOmegaSST and SpalartAllmaras both do, and fail to start without it.
wallDist
{{
    method          meshWave;
}}
"""
    return _write(case_dir, "system", "fvSchemes", "dictionary", body)


def write_solution(case_dir: str, steady: bool = False) -> str:
    """fvSolution.

    Steady runs use SIMPLE with under-relaxation and residual controls, so the
    run stops when it has actually converged rather than after a fixed count.
    Transient runs use PIMPLE with two pressure correctors per step.
    """
    if steady:
        algorithm = """
SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent      yes;

    residualControl
    {
        p               1e-3;
        U               1e-4;
        "(k|epsilon|omega|nuTilda)" 1e-4;
    }
}

relaxationFactors
{
    equations
    {
        U               0.9;
        ".*"            0.9;
    }
}
"""
    else:
        algorithm = """
PIMPLE
{
    nNonOrthogonalCorrectors 0;
    nCorrectors         2;
}
"""
    body = f"""
solvers
{{
    p
    {{
        solver          GAMG;
        tolerance       1e-7;
        relTol          0.01;
        smoother        DICGaussSeidel;
    }}

    pFinal
    {{
        $p;
        relTol          0;
    }}

    "(U|k|epsilon|omega|nuTilda)"
    {{
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-6;
        relTol          0.1;
    }}

    "(U|k|epsilon|omega|nuTilda)Final"
    {{
        $U;
        relTol          0;
    }}
}}
{algorithm}"""
    return _write(case_dir, "system", "fvSolution", "dictionary", body)


def write_physical(case_dir: str, nu: float, dia=None) -> str:
    """Kinematic viscosity, under whichever name this fork uses."""
    dia = dia or paths.openfoam_dialect()
    return _write(case_dir, "constant", dia.viscosity_dict, "dictionary",
                  dia.viscosity_body(_fmt(float(nu))))


def write_momentum_transport(case_dir: str, model: str, dia=None) -> str:
    """Turbulence model selection, under whichever name this fork uses."""
    dia = dia or paths.openfoam_dialect()
    if model == "laminar":
        body = "simulationType laminar;\n"
    else:
        body = (
            "simulationType RAS;\n\n"
            "RAS\n{\n"
            f"    {dia.turbulence_model_key}           {model};\n"
            "    turbulence      on;\n"
            "    printCoeffs     on;\n}\n"
        )
    return _write(case_dir, "constant", dia.turbulence_dict, "dictionary", body)


def force_coeffs_function(patch: str, speed: float, rho: float, area: float,
                          length: float, lift_dir=(0, 0, 1),
                          drag_dir=(1, 0, 0), centre=(0, 0, 0)) -> str:
    """A forceCoeffs function object: drag and lift coefficients, every step.

    Reported as coefficients rather than raw newtons because that is the form
    that transfers between scales -- and because `Aref`/`lRef` make the
    normalisation explicit instead of hidden.
    """
    return f"""    forceCoeffs
    {{
        type            forceCoeffs;
        libs            ("libforces.so");
        writeControl    timeStep;
        writeInterval   1;
        log             yes;

        patches         ({patch});
        rho             rhoInf;
        rhoInf          {_fmt(float(rho))};

        CofR            {_vec(centre)};
        liftDir         {_vec(lift_dir)};
        dragDir         {_vec(drag_dir)};
        pitchAxis       (0 1 0);

        magUInf         {_fmt(abs(float(speed)))};
        lRef            {_fmt(float(length))};
        Aref            {_fmt(float(area))};
    }}"""


# ---------------------------------------------------------------------------
# The whole case
# ---------------------------------------------------------------------------

def _reset_dir(path: str, attempts: int = 5) -> None:
    """Empty a case directory, working around WSL's lingering handles.

    A solver that has just run leaves the directory open on the Linux side for
    a moment, and Windows refuses to remove a directory anything still holds.
    Retrying briefly clears the common case; if the directory itself stays
    locked, emptying its contents is just as good -- what matters is that no
    stale mesh or time directory survives into the new case.
    """
    import time

    last: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            time.sleep(0.2 * (attempt + 1))

    for entry in os.listdir(path):
        target = os.path.join(path, entry)
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except OSError:
            raise RuntimeError(
                f"cannot clear the existing case at {path}: {last}\n"
                "  Something still has it open -- usually a solver or a shell "
                "sitting in that directory. Close it, or choose another name."
            )


def case_dir(name: str) -> str:
    if os.path.isabs(name):
        return name
    return os.path.join(config.out_dir(), "cases", os.path.basename(name))


def create(name: str, velocity, bounds: dict, cells=(40, 20, 20),
           nu: float = 1.5e-5, turbulence: str = "kEpsilon",
           two_d: bool = False, ground: bool = False,
           steady: bool = True, end_time: float = 500, write_interval: float = 100,
           delta_t: float = 1e-3, reference_length: float = 1.0,
           turbulence_intensity: float = 0.05,
           geometry_stl: str = "", surface_patch: str = "body",
           refinement: int = 2, layers: int = 0, inside_point=None,
           stl_scale: float = 0.001,
           force_coeffs: bool = False, reference_area: float = 1.0,
           rho: float = 1.225, overwrite: bool = True) -> dict:
    """Write a complete, runnable case.

    Defaults describe air at 20 C. `nu` is *kinematic* viscosity in m^2/s, and
    the solver is incompressible, so results are valid up to roughly Mach 0.3.

    `steady` (the default) solves for the final flow field and treats
    `end_time` as a maximum iteration count -- which is what you want for drag
    and lift. Set it False for transient behaviour such as vortex shedding, and
    `end_time` then means seconds.
    """
    dia = paths.openfoam_dialect()
    turbulence = turbulence.strip()
    if turbulence not in TURBULENCE_FIELDS:
        raise ValueError(
            f"unknown turbulence model {turbulence!r}. Available: "
            + ", ".join(TURBULENCE_FIELDS))

    path = case_dir(name)
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"case already exists: {path}")
        _reset_dir(path)
    os.makedirs(path, exist_ok=True)

    velocity = [float(v) for v in velocity]
    speed = math.sqrt(sum(v * v for v in velocity)) or 1e-9
    patches = box_patches(two_d, ground)

    if geometry_stl:
        stl_src = geometry_stl if os.path.isabs(geometry_stl) else os.path.join(
            config.out_dir(), geometry_stl)
        if not os.path.isfile(stl_src):
            raise FileNotFoundError(f"no STL at {stl_src}")
        tri_dir = os.path.join(path, "constant", "triSurface")
        os.makedirs(tri_dir, exist_ok=True)
        stl_name = os.path.basename(stl_src)
        shutil.copyfile(stl_src, os.path.join(tri_dir, stl_name))
        if inside_point is None:
            # A point just inside the inlet is fluid by construction, whatever
            # shape the geometry turns out to be.
            lo, hi = bounds["min"], bounds["max"]
            inside_point = [lo[0] + 0.01 * (hi[0] - lo[0]),
                            0.5 * (lo[1] + hi[1]),
                            0.5 * (lo[2] + hi[2])]
        write_snappy(path, stl_name, surface_patch, inside_point, refinement,
                     layers, stl_scale, dia)
        # snappyHexMesh creates this patch, so the 0/ fields must already
        # describe it or the solver will refuse to start.
        patches = patches + [Patch(surface_patch, "wall", foam_type="wall")]

    values = inlet_turbulence(speed, reference_length, turbulence_intensity)
    values["velocity"] = velocity

    write_block_mesh(path, bounds, cells, [p for p in patches if p.faces], dia)
    write_physical(path, nu, dia)
    write_momentum_transport(path, turbulence, dia)
    write_schemes(path, steady)
    write_solution(path, steady)

    functions = ""
    if force_coeffs:
        if not geometry_stl:
            raise ValueError(
                "force coefficients need a geometry to measure forces on; "
                "pass geometry_stl")
        functions = force_coeffs_function(
            surface_patch, speed, rho, reference_area, reference_length)
    write_control(path, end_time, write_interval, delta_t,
                  functions=functions, steady=steady, dia=dia)

    fields = ["U", "p"] + TURBULENCE_FIELDS[turbulence]
    for field in fields:
        write_field(path, field, patches, values)

    reynolds = speed * reference_length / nu
    with open(os.path.join(path, ".simkit"), "w", encoding="utf-8") as fh:
        # run.py needs to know which command to invoke and whether the case is
        # steady; both are decided here, so they are recorded here.
        fh.write(f"flavor={dia.flavor}\nsteady={int(bool(steady))}\n")
    return {
        "case": path,
        "openfoam": dia.describe(),
        "mode": "steady" if steady else "transient",
        "run_command": dia.run_command(steady),
        "fields": fields,
        "patches": [{"name": p.name, "role": p.role, "type": p.foam_type}
                    for p in patches],
        "turbulence": turbulence,
        "velocity_m_s": velocity,
        "speed_m_s": round(speed, 6),
        "reynolds": round(reynolds, 1),
        "inlet_k": round(values["k"], 8),
        "inlet_epsilon": round(values.get("epsilon", 0.0), 8),
        "cells_requested": int(cells[0]) * int(cells[1]) * int(cells[2]),
        "geometry": os.path.basename(geometry_stl) if geometry_stl else "",
        "force_coeffs": bool(force_coeffs),
    }
