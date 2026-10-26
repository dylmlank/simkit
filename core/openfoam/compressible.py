"""Compressible flow: nozzles, ducts and anything where density changes.

The incompressible case writer in `case.py` is fine for external aerodynamics
at road speeds. It is useless for a jet nozzle, because the whole point of a
nozzle is that the gas accelerates until it chokes, and an incompressible
solver has no mechanism to represent choking at all -- it will report a mass
flow that physically cannot pass through the hole.

So this module writes a genuinely compressible case: a thermodynamic model, a
temperature field, energy transport, and inlet conditions expressed the way
turbomachinery actually specifies them -- as *total* (stagnation) pressure and
temperature, which is what a combustor delivers, rather than a velocity, which
is what you are trying to find out.

Geometry here is axisymmetric. A nozzle, a diffuser and a duct are all bodies
of revolution, and OpenFOAM solves those on a one-cell-thick wedge with two
`wedge` patches, which is both far cheaper and far more accurate than a coarse
3-D mesh of the same thing.
"""
from __future__ import annotations

import math
import os

import config
from .. import paths
from .case import FOOTER, HEADER, _fmt, _reset_dir, _vec, _write, case_dir

#: Air. Molecular weight g/mol, cp J/kg/K, and Sutherland transport constants.
AIR = {
    "molWeight": 28.96,
    "Cp": 1005.0,
    "Hf": 0.0,
    "mu": 1.8e-05,
    "Pr": 0.71,
}

R_UNIVERSAL = 8314.5   # J/kmol/K
GAMMA = 1.4


def gas_constant(mol_weight: float = AIR["molWeight"]) -> float:
    return R_UNIVERSAL / mol_weight


def choked_mass_flow(total_pressure: float, total_temperature: float,
                     throat_area_m2: float, gamma: float = GAMMA,
                     mol_weight: float = AIR["molWeight"],
                     discharge_coefficient: float = 1.0) -> float:
    """Mass flow through a choked throat, from first principles.

    This is the closed-form answer a converged CFD run should reproduce. It is
    here to be compared against, not to replace the simulation: if the two
    disagree by more than a few percent, one of them is wrong and it is worth
    knowing which before cutting metal.
    """
    R = gas_constant(mol_weight)
    factor = math.sqrt(gamma / R) * (2.0 / (gamma + 1.0)) ** \
        ((gamma + 1.0) / (2.0 * (gamma - 1.0)))
    return (discharge_coefficient * throat_area_m2 * total_pressure * factor
            / math.sqrt(total_temperature))


def pressure_ratio_to_choke(gamma: float = GAMMA) -> float:
    """Total-to-static pressure ratio at which a convergent nozzle chokes."""
    return ((gamma + 1.0) / 2.0) ** (gamma / (gamma - 1.0))


# ---------------------------------------------------------------------------
# Axisymmetric wedge mesh
# ---------------------------------------------------------------------------

def write_wedge_mesh(case_path: str, stations: list, cells_axial: int = 120,
                     cells_radial: int = 30, wedge_angle: float = 5.0,
                     dia=None) -> dict:
    """A one-cell-thick wedge through a body of revolution.

    `stations` is [(z_mm, radius_mm), ...] along the axis, front to back. The
    mesh is built as a stack of blocks between consecutive stations, so a
    convergent nozzle is simply two stations with a smaller radius at the back.

    The wedge is centred on the axis and closed by two `wedge` patches, which is
    how OpenFOAM is told the geometry is axisymmetric. The half-angle must stay
    small -- OpenFOAM expects under about 5 degrees total -- because the cells
    at the axis degenerate into slivers as it grows.
    """
    dia = dia or paths.openfoam_dialect()
    if len(stations) < 2:
        raise ValueError("a wedge mesh needs at least two stations")
    for i in range(1, len(stations)):
        if stations[i][0] <= stations[i - 1][0]:
            raise ValueError(
                f"stations must advance along the axis; station {i} is at "
                f"z={stations[i][0]} but station {i - 1} is already at "
                f"{stations[i - 1][0]}")

    half = math.radians(wedge_angle) / 2.0
    sin_h, cos_h = math.sin(half), math.cos(half)

    # The wedge lies along +x and is symmetric about the x-y plane, which is
    # what OpenFOAM's wedge patch requires.
    #
    # Each station contributes three vertices, and the axis one is used TWICE
    # in the hex. That repetition is the whole trick: naming the same index on
    # both sides tells blockMesh the edge is collapsed, so it builds a prism.
    # Giving the axis two distinct indices at the same coordinates instead
    # produces zero-area faces, which checkMesh reports as infinite skewness
    # and which quietly wreck the solution near the centreline.
    #
    #   3i+0  axis        3i+1  rim, -wedge        3i+2  rim, +wedge
    verts, blocks, n = [], [], len(stations)
    for z_mm, r_mm in stations:
        z, r = z_mm / 1000.0, r_mm / 1000.0
        verts.append((z, 0.0, 0.0))
        verts.append((z, r * cos_h, -r * sin_h))
        verts.append((z, r * cos_h, r * sin_h))

    per_block = max(1, cells_axial // (n - 1))
    for i in range(n - 1):
        a, b = 3 * i, 3 * (i + 1)
        # local x axial, y radial, z azimuthal (collapsed onto the axis).
        blocks.append(
            f"    hex ({a} {b} {b+1} {a+1} {a} {b} {b+2} {a+2}) "
            f"({per_block} {cells_radial} 1) simpleGrading (1 1 1)")

    def faces(fn):
        return "\n".join("            " + fn(3 * i, 3 * (i + 1))
                         for i in range(n - 1))

    first, last = 0, 3 * (n - 1)
    boundary = f"""    inlet
    {{
        type            patch;
        faces           (({first} {first} {first+2} {first+1}));
    }}

    outlet
    {{
        type            patch;
        faces           (({last} {last+1} {last+2} {last}));
    }}

    wall
    {{
        type            wall;
        faces
        (
{faces(lambda a, b: f"({a+1} {a+2} {b+2} {b+1})")}
        );
    }}

    wedgeBack
    {{
        type            wedge;
        faces
        (
{faces(lambda a, b: f"({a} {a+1} {b+1} {b})")}
        );
    }}

    wedgeFront
    {{
        type            wedge;
        faces
        (
{faces(lambda a, b: f"({a} {b} {b+2} {a+2})")}
        );
    }}"""

    body = (
        f"{dia.scale_key} 1;\n\n"
        "vertices\n(\n"
        + "\n".join(f"    {_vec(v)}" for v in verts)
        + "\n);\n\nblocks\n(\n" + "\n".join(blocks) + "\n);\n\n"
        "edges\n(\n);\n\n"
        # With the axis edge collapsed the block has no y-min face at all, so
        # nothing should land here. Named anyway, so that if a future profile
        # does leave a face undefined it lands somewhere legible rather than in
        # an anonymous defaultFaces patch.
        "defaultPatch\n{\n    name            axis;\n"
        "    type            empty;\n}\n\n"
        f"boundary\n(\n{boundary}\n);\n\n"
        "mergePatchPairs\n(\n);"
    )
    _write(case_path, "system", "blockMeshDict", "dictionary", body)
    return {"stations": len(stations), "wedge_angle_deg": wedge_angle}


#: The wedge patches are geometry, not physics -- every field gets the same
#: treatment on them, whatever it is.
WEDGE_PATCHES = {"wedgeFront": "wedge", "wedgeBack": "wedge", "axis": "empty"}


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

def _field(case_path: str, name: str, cls: str, dims: str, internal: str,
           patches: dict) -> str:
    blocks = [f"    {patch}\n    {{\n{spec}\n    }}"
              for patch, spec in patches.items()]
    body = (f"dimensions      {dims};\n\n"
            f"internalField   uniform {internal};\n\n"
            "boundaryField\n{\n" + "\n\n".join(blocks) + "\n}")
    return _write(case_path, "0", name, cls, body)


def _wedge_blocks() -> dict:
    return {name: f"        type            {kind};"
            for name, kind in WEDGE_PATCHES.items()}


def write_fields(case_path: str, total_pressure: float,
                 total_temperature: float, back_pressure: float,
                 turbulence: str = "kOmegaSST",
                 turbulence_intensity: float = 0.05,
                 reference_length: float = 0.02) -> list:
    """The 0/ directory for a compressible internal-flow case.

    Note that pressure here is absolute, in pascals. The incompressible solver
    works in kinematic pressure (p/rho, m2/s2) and the two are not
    interchangeable -- feeding one solver the other's numbers produces a case
    that runs and is entirely meaningless.

    The inlet is specified as total pressure and total temperature because that
    is what a combustor delivers and what a test rig measures. The velocity is
    an *output*: it is set by the pressure the nozzle can drop, which is the
    question being asked.
    """
    written = []

    # --- velocity: pressure-driven, so the inlet velocity is computed ---
    written.append(_field(
        case_path, "U", "volVectorField", "[0 1 -1 0 0 0 0]", "(0 0 0)",
        {
            "inlet": ("        type            pressureInletOutletVelocity;\n"
                      "        value           uniform (0 0 0);"),
            "outlet": ("        type            inletOutlet;\n"
                       "        inletValue      uniform (0 0 0);\n"
                       "        value           uniform (0 0 0);"),
            "wall": "        type            noSlip;",
            **_wedge_blocks(),
        }))

    # --- pressure: total at inlet, static at outlet ---
    written.append(_field(
        case_path, "p", "volScalarField", "[1 -1 -2 0 0 0 0]",
        _fmt(back_pressure),
        {
            "inlet": ("        type            totalPressure;\n"
                      f"        p0              uniform {_fmt(total_pressure)};\n"
                      f"        value           uniform {_fmt(total_pressure)};"),
            "outlet": ("        type            fixedValue;\n"
                       f"        value           uniform {_fmt(back_pressure)};"),
            "wall": "        type            zeroGradient;",
            **_wedge_blocks(),
        }))

    # --- temperature ---
    written.append(_field(
        case_path, "T", "volScalarField", "[0 0 0 1 0 0 0]",
        _fmt(total_temperature),
        {
            # gamma is required here: the condition converts the total
            # temperature you specify into a static one using the local
            # velocity, and it cannot do that without the specific heat ratio.
            "inlet": ("        type            inletOutletTotalTemperature;\n"
                      f"        T0              uniform {_fmt(total_temperature)};\n"
                      f"        gamma           {_fmt(GAMMA)};\n"
                      "        inletValue       uniform "
                      f"{_fmt(total_temperature)};\n"
                      f"        value           uniform {_fmt(total_temperature)};"),
            "outlet": ("        type            inletOutlet;\n"
                       f"        inletValue      uniform {_fmt(total_temperature)};\n"
                       f"        value           uniform {_fmt(total_temperature)};"),
            # An uncooled liner runs close to gas temperature; treating the wall
            # as adiabatic is the conservative choice for nozzle performance.
            "wall": "        type            zeroGradient;",
            **_wedge_blocks(),
        }))

    # --- turbulent thermal diffusivity: compressible-only ---
    written.append(_field(
        case_path, "alphat", "volScalarField", "[1 -1 -1 0 0 0 0]", "0",
        {
            "inlet": ("        type            calculated;\n"
                      "        value           uniform 0;"),
            "outlet": ("        type            calculated;\n"
                       "        value           uniform 0;"),
            "wall": ("        type            compressible::alphatWallFunction;\n"
                     "        Prt             0.85;\n"
                     "        value           uniform 0;"),
            **_wedge_blocks(),
        }))

    if turbulence == "laminar":
        return written

    # Estimate the inlet turbulence from the speed the nozzle will reach, since
    # no inlet velocity was given -- the choked speed of sound is the right
    # order and the result is insensitive to it.
    speed = math.sqrt(GAMMA * gas_constant() * total_temperature) * 0.5
    length = max(0.07 * reference_length, 1e-6)
    k = 1.5 * (speed * turbulence_intensity) ** 2
    epsilon = (0.09 ** 0.75) * k ** 1.5 / length
    omega = k ** 0.5 / (0.09 ** 0.25 * length)

    written.append(_field(
        case_path, "nut", "volScalarField", "[0 2 -1 0 0 0 0]", "0",
        {
            "inlet": "        type            calculated;\n        value           uniform 0;",
            "outlet": "        type            calculated;\n        value           uniform 0;",
            "wall": ("        type            nutkWallFunction;\n"
                     "        value           uniform 0;"),
            **_wedge_blocks(),
        }))
    written.append(_field(
        case_path, "k", "volScalarField", "[0 2 -2 0 0 0 0]", _fmt(k),
        {
            "inlet": ("        type            inletOutlet;\n"
                      f"        inletValue      uniform {_fmt(k)};\n"
                      f"        value           uniform {_fmt(k)};"),
            "outlet": ("        type            inletOutlet;\n"
                       f"        inletValue      uniform {_fmt(k)};\n"
                       f"        value           uniform {_fmt(k)};"),
            "wall": ("        type            kqRWallFunction;\n"
                     f"        value           uniform {_fmt(k)};"),
            **_wedge_blocks(),
        }))
    second = "omega" if turbulence == "kOmegaSST" else "epsilon"
    value, dims, wall_fn = (
        (omega, "[0 0 -1 0 0 0 0]", "omegaWallFunction") if second == "omega"
        else (epsilon, "[0 2 -3 0 0 0 0]", "epsilonWallFunction"))
    written.append(_field(
        case_path, second, "volScalarField", dims, _fmt(value),
        {
            "inlet": ("        type            inletOutlet;\n"
                      f"        inletValue      uniform {_fmt(value)};\n"
                      f"        value           uniform {_fmt(value)};"),
            "outlet": ("        type            inletOutlet;\n"
                       f"        inletValue      uniform {_fmt(value)};\n"
                       f"        value           uniform {_fmt(value)};"),
            "wall": (f"        type            {wall_fn};\n"
                     f"        value           uniform {_fmt(value)};"),
            **_wedge_blocks(),
        }))
    return written


# ---------------------------------------------------------------------------
# constant/ and system/
# ---------------------------------------------------------------------------

def write_thermo(case_path: str, dia=None, mol_weight: float = AIR["molWeight"],
                 cp: float = AIR["Cp"], mu: float = AIR["mu"],
                 pr: float = AIR["Pr"]) -> str:
    """The thermodynamic model: perfect gas, constant cp, constant viscosity."""
    dia = dia or paths.openfoam_dialect()
    # hePsiThermo works from pressure and temperature, which is what a
    # pressure-driven nozzle case supplies.
    body = f"""
thermoType
{{
    type            hePsiThermo;
    mixture         pureMixture;
    transport       const;
    thermo          hConst;
    equationOfState perfectGas;
    specie          specie;
    energy          sensibleEnthalpy;
}}

mixture
{{
    specie
    {{
        molWeight       {_fmt(mol_weight)};
    }}
    thermodynamics
    {{
        Cp              {_fmt(cp)};
        Hf              0;
    }}
    transport
    {{
        mu              {_fmt(mu)};
        Pr              {_fmt(pr)};
    }}
}}
"""
    return _write(case_path, "constant", dia.thermo_dict, "dictionary", body)


def write_mrf(case_path: str, cell_zone: str, rpm: float,
              axis=(0, 0, 1), origin=(0, 0, 0),
              non_rotating_patches=("wall",)) -> str:
    """A rotating reference frame over one cell zone.

    This is how a compressor or turbine stage is solved steadily: the mesh does
    not move, but the equations inside the zone gain the Coriolis and
    centrifugal terms of a frame rotating at `rpm`. It needs a cell zone to
    apply to, which means the mesh must have been built with one (topoSet, or a
    separate region in snappyHexMesh).

    Nothing in a turbocharger-based engine currently uses this: the compressor
    and turbine wheels come with the turbo and SimKit has no geometry for them.
    It is here for when you scan or model a wheel.
    """
    patches = " ".join(non_rotating_patches)
    body = f"""
{cell_zone}
{{
    cellZone        {cell_zone};
    active          yes;

    // Patches that stay in the laboratory frame even though they bound the
    // rotating zone -- a shroud over a spinning wheel is the usual case.
    nonRotatingPatches ({patches});

    origin          {_vec(origin)};
    axis            {_vec(axis)};
    rpm             {_fmt(float(rpm))};
}}
"""
    return _write(case_path, "constant", "MRFProperties", "dictionary", body)


def write_constraints(case_path: str, min_temperature: float = 150.0,
                      max_temperature: float = 3000.0, dia=None) -> str:
    """Bound the solution so a bad transient cannot destroy the run.

    A steady compressible solver reaches its answer through states that are not
    physical. Early on, the pressure field is wrong everywhere, and the energy
    equation can hand the thermodynamics a negative temperature -- at which
    point the perfect-gas relations produce nonsense and the run aborts with
    "Negative initial temperature". Limiters clip those excursions without
    touching the converged answer, which sits far inside the bounds.
    """
    dia = dia or paths.openfoam_dialect()
    body = f"""
limitT
{{
    type            limitTemperature;
    selectionMode   all;
    min             {_fmt(min_temperature)};
    max             {_fmt(max_temperature)};
}}
"""
    return _write(case_path, "system", dia.constraints_dict, "dictionary", body)


def write_schemes(case_path: str, steady: bool = True) -> str:
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
    default         none;
    div(phi,U)      Gauss linearUpwind grad(U);
    div(phi,e)      Gauss upwind;
    div(phi,h)      Gauss upwind;
    div(phi,K)      Gauss upwind;
    div(phi,Ekp)    Gauss upwind;
    div(phid,p)     Gauss upwind;
    div(phi,k)      Gauss upwind;
    div(phi,epsilon) Gauss upwind;
    div(phi,omega)  Gauss upwind;
    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;
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

wallDist
{{
    method          meshWave;
}}
"""
    return _write(case_path, "system", "fvSchemes", "dictionary", body)


def write_solution(case_path: str, steady: bool = True) -> str:
    algorithm = """
SIMPLE
{
    nNonOrthogonalCorrectors 0;
    rhoMin          0.05;
    rhoMax          10;

    // Keep the pressure field inside a band around its initial value during
    // the transient. Without this the first few iterations can drive it
    // negative, and there is no recovering from that.
    pMinFactor      0.2;
    pMaxFactor      3.0;

    residualControl
    {
        p               1e-4;
        U               1e-4;
        e               1e-4;
        h               1e-4;
        "(k|epsilon|omega)" 1e-4;
    }
}

relaxationFactors
{
    fields
    {
        p               0.3;
        rho             0.05;
    }
    equations
    {
        U               0.5;
        "(e|h)"         0.5;
        "(k|epsilon|omega)" 0.5;
    }
}
""" if steady else """
PIMPLE
{
    nOuterCorrectors 2;
    nCorrectors      2;
    nNonOrthogonalCorrectors 0;
}
"""
    body = f"""
solvers
{{
    p
    {{
        solver          GAMG;
        tolerance       1e-8;
        relTol          0.01;
        smoother        GaussSeidel;
    }}

    "(U|e|h|k|epsilon|omega)"
    {{
        solver          PBiCGStab;
        preconditioner  DILU;
        tolerance       1e-8;
        relTol          0.1;
    }}

    "(U|e|h|k|epsilon|omega)Final"
    {{
        $U;
        relTol          0;
    }}

    rho
    {{
        solver          PCG;
        preconditioner  DIC;
        tolerance       1e-8;
        relTol          0;
    }}
}}
{algorithm}"""
    return _write(case_path, "system", "fvSolution", "dictionary", body)


def flow_functions(patches=("inlet", "outlet")) -> str:
    """Function objects reporting mass flow and total pressure at each patch.

    Mass flow is the number that decides whether a nozzle is the right size,
    and it is not something to read off a contour plot.
    """
    blocks = []
    for patch in patches:
        blocks.append(f"""    massFlow_{patch}
    {{
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   20;
        log             yes;
        writeFields     no;
        regionType      patch;
        name            {patch};
        operation       sum;
        fields          (phi);
    }}

    total_{patch}
    {{
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   20;
        log             yes;
        writeFields     no;
        regionType      patch;
        name            {patch};
        operation       areaAverage;
        fields          (p T U);
    }}""")
    return "\n\n".join(blocks)


def write_control(case_path: str, end_time: int = 800, write_interval: int = 800,
                  steady: bool = True, functions: str = "", dia=None) -> str:
    dia = dia or paths.openfoam_dialect()
    fn = f"\nfunctions\n{{\n{functions}\n}}\n" if functions else ""
    body = f"""
{dia.control_solver_key}          {dia.compressible_solver(steady)};

startFrom       latestTime;
startTime       0;
stopAt          endTime;
endTime         {int(end_time)};
deltaT          1;

writeControl    timeStep;
writeInterval   {int(write_interval)};
purgeWrite      0;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable yes;
{fn}"""
    return _write(case_path, "system", "controlDict", "dictionary", body)


# ---------------------------------------------------------------------------

def create_nozzle_case(name: str, stations: list, total_pressure: float,
                       total_temperature: float,
                       back_pressure: float = 101325.0,
                       turbulence: str = "kOmegaSST",
                       cells_axial: int = 140, cells_radial: int = 34,
                       end_time: int = 800, overwrite: bool = True) -> dict:
    """A complete axisymmetric compressible case for a nozzle or duct.

    `stations` is [(z_mm, radius_mm), ...] describing the internal profile from
    inlet to exit. Pressures are absolute pascals; temperatures kelvin.
    """
    dia = paths.openfoam_dialect()
    path = case_dir(name)
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"case already exists: {path}")
        _reset_dir(path)
    os.makedirs(path, exist_ok=True)

    if total_pressure <= back_pressure:
        raise ValueError(
            f"the inlet total pressure ({total_pressure:,.0f} Pa) is not above "
            f"the back pressure ({back_pressure:,.0f} Pa), so nothing will "
            "flow. Check which is which.")

    exit_radius = stations[-1][1] / 1000.0
    exit_area = math.pi * exit_radius ** 2
    reference_length = 2 * exit_radius

    mesh = write_wedge_mesh(path, stations, cells_axial, cells_radial, dia=dia)
    write_thermo(path, dia)
    from .case import write_momentum_transport
    write_momentum_transport(path, turbulence, dia)
    write_schemes(path, steady=True)
    write_solution(path, steady=True)
    # Bound temperature generously around the inlet condition: wide enough not
    # to touch the answer, tight enough to catch a diverging transient.
    write_constraints(path, min_temperature=max(150.0, 0.2 * total_temperature),
                      max_temperature=max(2000.0, 2.5 * total_temperature),
                      dia=dia)
    write_fields(path, total_pressure, total_temperature, back_pressure,
                 turbulence, reference_length=reference_length)
    write_control(path, end_time, end_time, True, flow_functions(), dia)

    with open(os.path.join(path, ".simkit"), "w", encoding="utf-8") as fh:
        fh.write(f"flavor={dia.flavor}\nsteady=1\ncompressible=1\n")

    ratio = total_pressure / back_pressure
    choke_ratio = pressure_ratio_to_choke()
    predicted = choked_mass_flow(total_pressure, total_temperature, exit_area)

    return {
        "case": path,
        "openfoam": dia.describe(),
        "run_command": dia.run_command(True, compressible=True),
        "stations": mesh["stations"],
        "wedge_angle_deg": mesh["wedge_angle_deg"],
        "exit_diameter_mm": round(2000 * exit_radius, 3),
        "exit_area_mm2": round(exit_area * 1e6, 2),
        "total_pressure_Pa": round(total_pressure, 1),
        "total_temperature_K": round(total_temperature, 1),
        "back_pressure_Pa": round(back_pressure, 1),
        "pressure_ratio": round(ratio, 4),
        "chokes": ratio >= choke_ratio,
        "choking_ratio": round(choke_ratio, 4),
        "predicted_choked_mass_flow_kg_s": round(predicted, 6),
        "note": (
            "the predicted mass flow is the ideal choked value from theory, "
            "for comparison with what the solver finds. A real nozzle passes "
            "less: expect a discharge coefficient around 0.95-0.98."),
    }
