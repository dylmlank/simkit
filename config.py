"""Shared settings for SimKit.

SimKit connects Claude to four real engineering applications:

  * Autodesk Fusion 360 - live, through an add-in that runs inside Fusion
  * FreeCAD             - headless, through freecadcmd.exe
  * OpenRocket          - headless, through its own bundled JRE and jar
  * OpenFOAM            - headless, through WSL

Every path below is auto-discovered at runtime (see core/paths.py); the
environment variables here only exist to override a discovery that got it
wrong, or to point at a second install.
"""
from __future__ import annotations

import os

# Where every generated file lands: FreeCAD documents, STEP/STL exports, .ork
# designs, OpenFOAM cases, reports. Tools never write outside this directory.
OUT_DIR = os.environ.get("SIMKIT_OUT", os.path.join(os.path.dirname(__file__), "out"))

# --- Fusion 360 -------------------------------------------------------------
# The add-in listens on this port inside Fusion. Loopback only: the add-in
# executes arbitrary CAD operations, so it must never be reachable off-box.
FUSION_HOST = os.environ.get("SIMKIT_FUSION_HOST", "127.0.0.1")
FUSION_PORT = int(os.environ.get("SIMKIT_FUSION_PORT", "9720"))
# Fusion rebuilds can be slow; a big parametric change may take a while.
FUSION_TIMEOUT = float(os.environ.get("SIMKIT_FUSION_TIMEOUT", "120"))

# --- FreeCAD ----------------------------------------------------------------
# Full path to freecadcmd.exe. Empty means "discover it".
FREECAD_CMD = os.environ.get("SIMKIT_FREECAD", "")
FREECAD_TIMEOUT = float(os.environ.get("SIMKIT_FREECAD_TIMEOUT", "300"))

# --- OpenRocket -------------------------------------------------------------
# Directory of the OpenRocket install (the one containing jar\ and jre\).
OPENROCKET_HOME = os.environ.get("SIMKIT_OPENROCKET", "")
# OpenRocket ships its own Java. Using it avoids depending on a system JDK and
# guarantees the version the jar was built against.
OPENROCKET_JVM = os.environ.get("SIMKIT_OPENROCKET_JVM", "")

# --- OpenFOAM ---------------------------------------------------------------
# OpenFOAM is Linux-only, so it runs under WSL. Empty distro = WSL's default.
WSL_DISTRO = os.environ.get("SIMKIT_WSL_DISTRO", "")
# Path *inside* WSL to the OpenFOAM etc/bashrc that sets up the environment.
OPENFOAM_BASHRC = os.environ.get("SIMKIT_OPENFOAM_BASHRC", "")
# Solvers can run for a long time; this is a hard ceiling per run.
OPENFOAM_TIMEOUT = float(os.environ.get("SIMKIT_OPENFOAM_TIMEOUT", "1800"))

# --- Agent ------------------------------------------------------------------
MODEL = os.environ.get("SIMKIT_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("SIMKIT_MAX_TOKENS", "8192"))


def out_dir() -> str:
    """The output directory, created on first use."""
    os.makedirs(OUT_DIR, exist_ok=True)
    return OUT_DIR
