"""Reaching OpenFOAM, which only exists inside WSL.

Two things have to be bridged. Paths: a case lives on the Windows side (so it
stays visible to the user and to ParaView) but the solver addresses it as
`/mnt/<drive>/...`. And environment: OpenFOAM only works after its `etc/bashrc`
has been sourced, which sets ~40 variables and puts the solvers on PATH -- so
every command runs through a login shell that sources it first.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

import config
from .. import paths

_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


@dataclass
class Ran:
    """The outcome of one OpenFOAM command."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, lines: int = 25) -> str:
        text = (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")
        return "\n".join(text.strip().splitlines()[-lines:])

    def require(self) -> "Ran":
        if self.ok:
            return self
        raise RuntimeError(
            f"OpenFOAM command failed: {self.command}\n"
            f"  exit code {self.returncode}\n"
            f"  last output:\n{_indent(self.tail())}"
            + (f"\n  full log: {self.log_path}" if self.log_path else "")
        )


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def to_wsl(win_path: str) -> str:
    """Translate a Windows path to its WSL equivalent.

    Done in Python for the ordinary drive-letter case (a subprocess per path
    would be absurd when every run translates several), falling back to
    `wslpath` for anything unusual such as a UNC share.
    """
    win_path = os.path.abspath(win_path)
    m = _DRIVE.match(win_path)
    if m:
        drive, rest = m.groups()
        return f"/mnt/{drive.lower()}/" + rest.replace("\\", "/")
    out = paths.wsl_capture(f"wslpath -a {shquote(win_path)}").strip()
    if not out:
        raise ValueError(f"cannot map {win_path!r} into WSL")
    return out


def shquote(text: str) -> str:
    """Single-quote for POSIX sh (Windows paths are full of characters sh eats)."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def run(command: str, case_dir: str = "", timeout: float | None = None,
        log_name: str = "") -> Ran:
    """Run a command with the OpenFOAM environment sourced.

    `case_dir` is a Windows path; the command executes inside it. When
    `log_name` is given the combined output is also written to that file in the
    case directory, which is where OpenFOAM users expect to find it.
    """
    bashrc = paths.find_openfoam().require().extra["bashrc"]
    parts = [f"source {shquote(bashrc)} >/dev/null 2>&1"]
    if case_dir:
        parts.append(f"cd {shquote(to_wsl(case_dir))}")
    parts.append(command)
    script = " && ".join(parts)

    try:
        proc = subprocess.run(
            paths.wsl_argv() + ["-e", "bash", "-lc", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout or config.OPENFOAM_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        limit = timeout or config.OPENFOAM_TIMEOUT
        partial = (exc.stdout or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        raise RuntimeError(
            f"OpenFOAM command exceeded {limit:.0f}s and was stopped: {command}\n"
            "  Either the case is genuinely that big (raise SIMKIT_OPENFOAM_TIMEOUT)\n"
            "  or it is diverging (check the residuals).\n"
            + _indent("\n".join(partial.strip().splitlines()[-15:]))
        )

    log_path = ""
    if log_name and case_dir:
        log_path = os.path.join(case_dir, log_name)
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(proc.stdout)
                if proc.stderr:
                    fh.write("\n--- stderr ---\n" + proc.stderr)
        except OSError:
            log_path = ""

    return Ran(command, proc.returncode, proc.stdout, proc.stderr, log_path)
