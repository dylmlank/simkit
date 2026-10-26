"""The two OpenFOAM forks speak different dictionary dialects.

OpenFOAM split into two actively developed lines that share a name, a mesh
format and most of a solver library, but disagree about what the case files are
called:

                        Foundation (openfoam.org)   ESI (openfoam.com)
    version             13                          v2412
    viscosity           constant/physicalProperties constant/transportProperties
    turbulence          constant/momentumTransport  constant/turbulenceProperties
    turbulence model    model kEpsilon;             RASModel kEpsilon;
    controlDict         solver incompressibleFluid; application simpleFoam;
    run command         foamRun                     simpleFoam / pimpleFoam

A case written in one dialect fails in the other -- usually with a "cannot find
file" that gives no hint that the fork is the problem. This machine happens to
have both installed, which is exactly why SimKit detects the fork and writes to
match rather than assuming one.
"""
from __future__ import annotations

from dataclasses import dataclass

FOUNDATION = "foundation"
ESI = "esi"


@dataclass(frozen=True)
class Dialect:
    flavor: str
    version: str = ""

    # --- file names ---
    @property
    def viscosity_dict(self) -> str:
        return "physicalProperties" if self.is_foundation else "transportProperties"

    @property
    def turbulence_dict(self) -> str:
        return "momentumTransport" if self.is_foundation else "turbulenceProperties"

    @property
    def thermo_dict(self) -> str:
        """Where the thermodynamic model lives, for a compressible case.

        Foundation reuses one filename for both regimes -- `physicalProperties`
        holds a `viscosityModel` for incompressible flow and a `thermoType` for
        compressible. ESI keeps them apart. So the name alone does not tell you
        which kind of case you are looking at on the Foundation side.
        """
        return "physicalProperties" if self.is_foundation else "thermophysicalProperties"

    @property
    def is_foundation(self) -> bool:
        return self.flavor == FOUNDATION

    # --- dictionary keys ---
    @property
    def turbulence_model_key(self) -> str:
        return "model" if self.is_foundation else "RASModel"

    @property
    def control_solver_key(self) -> str:
        """Foundation names a solver *module*; ESI names an executable."""
        return "solver" if self.is_foundation else "application"

    @property
    def scale_key(self) -> str:
        """`convertToMeters` was renamed `scale`; ESI warns loudly about the old one."""
        return "convertToMeters" if self.is_foundation else "scale"

    @property
    def post_process_command(self) -> str:
        """The utility that re-runs function objects over saved results.

        Foundation renamed it `foamPostProcess` when it unified the solvers;
        ESI still calls it `postProcess`. Assuming either one gives a bare
        "command not found" that says nothing about forks.
        """
        return "foamPostProcess" if self.is_foundation else "postProcess"

    @property
    def constraints_dict(self) -> str:
        """Where bounds on the solution live -- temperature and pressure limits.

        Foundation calls the file `fvConstraints`, ESI `fvOptions`. Both hold
        the same limiters, and a compressible startup transient will happily
        drive temperature negative without them.
        """
        return "fvConstraints" if self.is_foundation else "fvOptions"

    @property
    def inside_point_key(self) -> str:
        """The seed point telling snappyHexMesh which side of a surface is fluid.

        Foundation calls it `insidePoint`, ESI `locationInMesh`. Neither errors
        on the other's spelling -- ESI merely notes that no location was
        provided and then deletes the entire mesh, so getting this wrong fails
        several minutes later with a message that names no keyword at all.
        """
        return "insidePoint" if self.is_foundation else "locationInMesh"

    def viscosity_body(self, nu: str) -> str:
        if self.is_foundation:
            return ("viscosityModel  constant;\n\n"
                    f"nu              [0 2 -1 0 0 0 0] {nu};\n")
        return ("transportModel  Newtonian;\n\n"
                f"nu              [0 2 -1 0 0 0 0] {nu};\n")

    # --- solver selection ---
    def solver_name(self, steady: bool) -> str:
        if self.is_foundation:
            # One module covers both; the time scheme decides which it behaves as.
            return "incompressibleFluid"
        return "simpleFoam" if steady else "pimpleFoam"

    def compressible_solver(self, steady: bool) -> str:
        """The solver for a case where density varies.

        A jet nozzle is the reason this matters: the flow accelerates until it
        chokes, and an incompressible solver has no mechanism to represent
        that. It will happily report a mass flow that cannot physically pass.
        """
        if self.is_foundation:
            return "fluid"
        return "rhoSimpleFoam" if steady else "rhoPimpleFoam"

    def run_command(self, steady: bool, compressible: bool = False) -> str:
        if self.is_foundation:
            return "foamRun"
        if compressible:
            return self.compressible_solver(steady)
        return self.solver_name(steady)

    def describe(self) -> str:
        origin = "openfoam.org" if self.is_foundation else "openfoam.com"
        return f"OpenFOAM {self.version or '?'} ({origin})"


def detect(has_foam_run: bool, version: str = "") -> Dialect:
    """Identify the fork.

    Keyed on `foamRun` rather than on the version string: the presence of the
    unified runner is precisely the thing that distinguishes Foundation 11+
    from every ESI release, and version numbering (13 vs v2412) is not
    comparable across the two lines.
    """
    return Dialect(FOUNDATION if has_foam_run else ESI, version)
