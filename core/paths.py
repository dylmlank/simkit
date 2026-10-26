"""Finding the four applications on this machine.

Each `find_*` returns a `Found`. A connector that cannot locate its application
says so plainly, with the places it looked and the thing to install or set --
it never falls back to a guess that will fail later with a confusing error.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field

import config

# WSL and Java calls are cheap but not free, and discovery runs on every tool
# call. Results are cached for the life of the process; a newly installed
# application is picked up on the next restart.
_cache: dict[str, "Found"] = {}


@dataclass
class Found:
    """The outcome of looking for one application."""

    name: str
    ok: bool
    path: str = ""
    version: str = ""
    detail: str = ""
    #: Every location that was checked, so a failure is actionable.
    looked_in: list[str] = field(default_factory=list)
    #: Extra discovered paths a connector needs (jar, jvm, add-in dir...).
    extra: dict[str, str] = field(default_factory=dict)

    def require(self) -> "Found":
        """Return self, or raise with the full search trail."""
        if self.ok:
            return self
        where = "\n".join(f"    {p}" for p in self.looked_in)
        raise RuntimeError(
            f"{self.name} was not found.\n"
            f"  {self.detail}\n"
            f"  Looked in:\n{where}"
        )

    def as_dict(self) -> dict:
        return {
            "application": self.name,
            "found": self.ok,
            "path": self.path,
            "version": self.version,
            "detail": self.detail,
            **({"extra": self.extra} if self.extra else {}),
        }


def _cached(key: str, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


def _first_glob(patterns: list[str]) -> str:
    """Newest match wins, so FreeCAD 1.1 beats a leftover FreeCAD 0.21."""
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(glob.glob(pattern))
    if not hits:
        return ""
    return sorted(hits)[-1]


# ---------------------------------------------------------------------------
# FreeCAD
# ---------------------------------------------------------------------------

FREECAD_PATTERNS = [
    r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe",
    r"C:\Program Files (x86)\FreeCAD*\bin\freecadcmd.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD*\bin\freecadcmd.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\FreeCAD*\bin\FreeCADCmd.exe"),
]


def find_freecad() -> Found:
    return _cached("freecad", _find_freecad)


def _find_freecad() -> Found:
    looked = list(FREECAD_PATTERNS)
    exe = config.FREECAD_CMD
    if exe:
        looked.insert(0, f"$SIMKIT_FREECAD = {exe}")
    if not exe or not os.path.isfile(exe):
        exe = _first_glob(FREECAD_PATTERNS) or shutil.which("freecadcmd") or ""
    if not exe:
        return Found(
            "FreeCAD",
            False,
            detail="Install FreeCAD, or set SIMKIT_FREECAD to freecadcmd.exe.",
            looked_in=looked,
        )
    # The install directory name carries the version; asking the binary costs a
    # process start and freecadcmd has no cheap --version that skips module init.
    home = os.path.dirname(os.path.dirname(exe))
    version = os.path.basename(home).replace("FreeCAD", "").strip() or "unknown"
    return Found("FreeCAD", True, exe, version, "headless via freecadcmd", looked)


# ---------------------------------------------------------------------------
# OpenRocket
# ---------------------------------------------------------------------------

OPENROCKET_PATTERNS = [
    r"C:\Program Files\OpenRocket",
    r"C:\Program Files (x86)\OpenRocket",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenRocket"),
]


def find_openrocket() -> Found:
    return _cached("openrocket", _find_openrocket)


def _find_openrocket() -> Found:
    looked = list(OPENROCKET_PATTERNS)
    home = config.OPENROCKET_HOME
    if home:
        looked.insert(0, f"$SIMKIT_OPENROCKET = {home}")
    if not home or not os.path.isdir(home):
        home = next((p for p in OPENROCKET_PATTERNS if os.path.isdir(p)), "")
    if not home:
        return Found(
            "OpenRocket",
            False,
            detail="Install OpenRocket, or set SIMKIT_OPENROCKET to its folder.",
            looked_in=looked,
        )

    jars = sorted(glob.glob(os.path.join(home, "jar", "*.jar")))
    if not jars:
        # A .jar-only download has no jar/ subdirectory.
        jars = sorted(glob.glob(os.path.join(home, "OpenRocket*.jar")))
    if not jars:
        return Found(
            "OpenRocket",
            False,
            path=home,
            detail=f"No OpenRocket jar under {home}.",
            looked_in=looked,
        )
    jar = jars[-1]

    # Prefer the JRE that shipped with OpenRocket: it is guaranteed to be the
    # version the jar was built against, and it is present even when the machine
    # has no system Java at all (this one does not).
    jvm = config.OPENROCKET_JVM
    if not jvm or not os.path.isfile(jvm):
        jvm = _first_glob([
            os.path.join(home, "jre", "bin", "server", "jvm.dll"),
            os.path.join(home, "jre", "bin", "client", "jvm.dll"),
        ])

    version = os.path.basename(jar).removeprefix("OpenRocket-").removesuffix(".jar")
    return Found(
        "OpenRocket",
        True,
        home,
        version,
        "headless via bundled JRE" if jvm else "found, but no bundled JVM",
        looked,
        {"jar": jar, "jvm": jvm},
    )


# ---------------------------------------------------------------------------
# Fusion 360
# ---------------------------------------------------------------------------

def find_fusion() -> Found:
    return _cached("fusion", _find_fusion)


def _find_fusion() -> Found:
    """Locate Fusion's add-ins directory.

    Fusion has no headless mode and no external API: the only way in is an
    add-in running inside the running application. So "found" here means the
    add-in directory exists, not that Fusion is currently open -- checking that
    is the bridge's job (`core/fusion/bridge.py`).
    """
    addins = os.path.expandvars(
        r"%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"
    )
    # Autodesk renamed the product folder from "Autodesk Fusion 360" to
    # "Autodesk Fusion"; support whichever this install uses.
    alt = os.path.expandvars(r"%APPDATA%\Autodesk\Autodesk Fusion\API\AddIns")
    looked = [addins, alt]
    target = next((p for p in (addins, alt) if os.path.isdir(p)), "")
    if not target:
        return Found(
            "Fusion 360",
            False,
            detail="Fusion 360 does not appear to be installed for this user.",
            looked_in=looked,
        )
    installed = os.path.isdir(os.path.join(target, "SimKit"))
    return Found(
        "Fusion 360",
        True,
        target,
        "",
        "add-in installed" if installed else "add-in NOT installed - run install_fusion_addin",
        looked,
        {"addin_installed": str(installed)},
    )


# ---------------------------------------------------------------------------
# OpenFOAM (under WSL)
# ---------------------------------------------------------------------------

#: Both upstreams, newest first. The Foundation build (openfoam.org) installs to
#: /opt/openfoamNN; the ESI build (openfoam.com) to /usr/lib/openfoam/openfoamNNNN.
OPENFOAM_BASHRC_GLOBS = "/opt/openfoam*/etc/bashrc /usr/lib/openfoam/openfoam*/etc/bashrc"


def find_openfoam() -> Found:
    return _cached("openfoam", _find_openfoam)


def _find_openfoam() -> Found:
    looked = ["wsl.exe", OPENFOAM_BASHRC_GLOBS]
    if not shutil.which("wsl"):
        return Found(
            "OpenFOAM",
            False,
            detail="WSL is not installed. OpenFOAM is Linux-only and runs under WSL.",
            looked_in=looked,
        )

    # `ls` on a glob that matches nothing prints an error and exits non-zero,
    # hence the 2>/dev/null; an empty result is the "not installed" signal.
    available = [line.strip() for line in wsl_capture(
        f"ls -d {OPENFOAM_BASHRC_GLOBS} 2>/dev/null | sort").splitlines()
        if line.strip()]

    bashrc = config.OPENFOAM_BASHRC
    if bashrc:
        looked.insert(0, f"$SIMKIT_OPENFOAM_BASHRC = {bashrc}")
    elif available:
        # Both forks can be installed side by side (this machine has each).
        # Newest path wins deterministically; SIMKIT_OPENFOAM_BASHRC overrides.
        bashrc = available[-1]
    if not bashrc:
        return Found(
            "OpenFOAM",
            False,
            detail=(
                "No OpenFOAM found inside WSL. Install it in the distro, e.g.\n"
                "    wsl sudo apt install openfoam13\n"
                "  or set SIMKIT_OPENFOAM_BASHRC to its etc/bashrc path."
            ),
            looked_in=looked,
        )

    # One shell call for both facts: the version, and whether the unified
    # `foamRun` exists -- which is what tells the two forks apart.
    probe = wsl_capture(
        f"source {bashrc} >/dev/null 2>&1; echo $WM_PROJECT_VERSION; "
        '[ -x "$FOAM_APPBIN/foamRun" ] && echo foamRun || echo nofoamRun'
    ).splitlines()
    version = probe[0].strip() if probe else ""
    has_foam_run = len(probe) > 1 and probe[1].strip() == "foamRun"

    from .openfoam.dialect import detect
    flavor = detect(has_foam_run, version)

    return Found(
        "OpenFOAM",
        True,
        bashrc,
        version or "unknown",
        f"{flavor.describe()}, headless via WSL "
        f"({wsl_distro() or 'default distro'})",
        looked,
        {
            "bashrc": bashrc,
            "flavor": flavor.flavor,
            "installs": " | ".join(available) if len(available) > 1 else bashrc,
        },
    )


def openfoam_dialect():
    """The dialect of the OpenFOAM this machine will actually run."""
    from .openfoam.dialect import Dialect
    found = find_openfoam().require()
    return Dialect(found.extra["flavor"], found.version)


def wsl_distro() -> str:
    return config.WSL_DISTRO


def wsl_argv() -> list[str]:
    """The `wsl.exe ...` prefix, honouring a pinned distro."""
    argv = ["wsl.exe"]
    if config.WSL_DISTRO:
        argv += ["-d", config.WSL_DISTRO]
    return argv


def wsl_capture(script: str, timeout: float = 60.0) -> str:
    """Run a bash snippet in WSL and return stdout ('' on any failure)."""
    try:
        proc = subprocess.run(
            wsl_argv() + ["-e", "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


# ---------------------------------------------------------------------------

def survey() -> list[Found]:
    """Look for all four applications."""
    return [find_fusion(), find_freecad(), find_openrocket(), find_openfoam()]
