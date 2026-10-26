"""Talking to the SimKit add-in running inside Fusion.

One newline-delimited JSON request per connection, which keeps the add-in side
trivial and means a hung command cannot poison a long-lived socket. The add-in
listens on loopback only -- it executes arbitrary CAD operations and must never
be reachable from off the machine.
"""
from __future__ import annotations

import json
import os
import shutil
import socket

import config
from .. import paths

#: Where Fusion looks for add-ins, relative to the add-ins directory.
ADDIN_NAME = "SimKit"


def _source_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "fusion_addin", ADDIN_NAME)


def install_addin(overwrite: bool = True) -> dict:
    """Copy the add-in into Fusion's add-ins directory.

    Fusion still has to be told to run it: Utilities > Add-Ins > the Add-Ins
    tab, select SimKit, Run (and tick "Run on Startup"). There is no API for
    installing an add-in from outside, so this is as far as automation goes.
    """
    found = paths.find_fusion().require()
    source = _source_dir()
    if not os.path.isdir(source):
        raise FileNotFoundError(f"the add-in source is missing from {source}")

    target = os.path.join(found.path, ADDIN_NAME)
    if os.path.isdir(target):
        if not overwrite:
            raise FileExistsError(f"already installed at {target}")
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return {
        "installed_to": target,
        "next_step": (
            "In Fusion: Utilities tab > Add-Ins > Add-Ins tab > select SimKit "
            "> Run. Tick 'Run on Startup' to avoid repeating this."
        ),
    }


def is_installed() -> bool:
    found = paths.find_fusion()
    return found.ok and os.path.isdir(os.path.join(found.path, ADDIN_NAME))


def send(command: str, params: dict | None = None,
         timeout: float | None = None) -> dict:
    """Run one command inside Fusion and return its result."""
    timeout = timeout or config.FUSION_TIMEOUT
    request = json.dumps(
        {"command": command, "params": params or {}, "timeout": timeout}) + "\n"

    try:
        with socket.create_connection(
                (config.FUSION_HOST, config.FUSION_PORT), timeout=10) as sock:
            # The socket timeout has to cover the whole operation, not just the
            # connect: a rebuild can legitimately take a minute.
            sock.settimeout(timeout + 10)
            sock.sendall(request.encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
    except ConnectionRefusedError:
        raise RuntimeError(_not_listening())
    except socket.timeout:
        raise RuntimeError(
            f"Fusion accepted the command but did not answer within {timeout:.0f}s. "
            "It is probably showing a dialog -- check the Fusion window."
        )
    except OSError as exc:
        raise RuntimeError(f"could not reach Fusion: {exc}\n\n{_not_listening()}")

    payload = b"".join(chunks).decode("utf-8", "replace").strip()
    if not payload:
        raise RuntimeError("Fusion closed the connection without replying")
    try:
        response = json.loads(payload)
    except json.JSONDecodeError:
        raise RuntimeError(f"Fusion sent something that is not JSON: {payload[:500]}")

    if not response.get("ok"):
        detail = response.get("error", "unknown error")
        raise RuntimeError(f"Fusion error -- {detail}")
    return response.get("result", {})


def _not_listening() -> str:
    installed = is_installed()
    lines = [
        f"Nothing is listening on {config.FUSION_HOST}:{config.FUSION_PORT}, so "
        "Fusion is not reachable.",
        "  Fusion has no headless mode: it must be open, with a design "
        "document, and the SimKit add-in running.",
    ]
    if not installed:
        lines.append("  The add-in is not installed yet -- run fusion_install_addin.")
    else:
        lines.append(
            "  The add-in is installed. In Fusion: Utilities > Add-Ins > "
            "Add-Ins tab > SimKit > Run.")
    return "\n".join(lines)


def status() -> dict:
    """Whether Fusion is reachable right now, without raising if it is not."""
    found = paths.find_fusion()
    out = {
        "fusion_installed": found.ok,
        "addin_installed": is_installed(),
        "address": f"{config.FUSION_HOST}:{config.FUSION_PORT}",
        "connected": False,
    }
    if not found.ok:
        out["detail"] = found.detail
        return out
    try:
        out.update(send("ping", timeout=15))
        out["connected"] = True
    except RuntimeError as exc:
        out["detail"] = str(exc)
    return out
