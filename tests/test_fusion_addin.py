"""The Fusion add-in's transport, tested without Fusion.

Fusion cannot be scripted from outside and will not start headlessly, so the
add-in's risky machinery -- the socket protocol and the hand-off from a socket
thread to Fusion's main thread -- is exercised here against a stub of the API.
That covers everything except the CAD calls themselves, which only the real
application can answer for.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import types

import pytest

ADDIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "fusion_addin", "SimKit", "SimKit.py")


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)

    def remove(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)


class _FakeArgs:
    def __init__(self, token):
        self.additionalInfo = token


class _FakeApp:
    """Stands in for Fusion, including its main-thread event dispatch."""

    version = "2.0.99999"

    def __init__(self):
        self.events = {}
        self.userInterface = types.SimpleNamespace(messageBox=lambda *a: None)
        self.activeDocument = types.SimpleNamespace(name="Stub Document")
        self.activeProduct = None
        self.logged = []

    def registerCustomEvent(self, event_id):
        self.events[event_id] = _FakeEvent()
        return self.events[event_id]

    def unregisterCustomEvent(self, event_id):
        self.events.pop(event_id, None)

    def fireCustomEvent(self, event_id, token):
        # Real Fusion runs handlers on its main thread, asynchronously. A
        # separate thread reproduces that: the socket thread must not be the
        # one executing the command, or the test would pass even if the
        # hand-off were missing entirely.
        event = self.events[event_id]

        def deliver():
            for handler in event.handlers:
                handler.notify(_FakeArgs(token))

        threading.Thread(target=deliver, daemon=True).start()

    def log(self, message):
        self.logged.append(message)


@pytest.fixture(scope="module")
def addin():
    """Import the add-in with a stubbed `adsk` package."""
    core = types.ModuleType("adsk.core")
    core.Application = types.SimpleNamespace(get=lambda: _app)
    core.CustomEventHandler = type("CustomEventHandler", (object,), {})
    core.Point3D = types.SimpleNamespace(create=lambda *a: a)
    core.Vector3D = types.SimpleNamespace(create=lambda *a: a)
    core.Matrix3D = types.SimpleNamespace(create=lambda: types.SimpleNamespace())
    core.ObjectCollection = types.SimpleNamespace(create=lambda: [])
    core.ValueInput = types.SimpleNamespace(
        createByReal=lambda v: v, createByString=lambda v: v)
    core.DocumentTypes = types.SimpleNamespace(FusionDesignDocumentType=0)

    fusion_mod = types.ModuleType("adsk.fusion")
    fusion_mod.Design = types.SimpleNamespace(cast=lambda product: None)
    fusion_mod.FeatureOperations = types.SimpleNamespace(
        NewBodyFeatureOperation=0, CutFeatureOperation=1,
        JoinFeatureOperation=2, IntersectFeatureOperation=3)
    fusion_mod.DesignTypes = types.SimpleNamespace(ParametricDesignType=1)
    fusion_mod.MeshRefinementSettings = types.SimpleNamespace(
        MeshRefinementLow=0, MeshRefinementMedium=1, MeshRefinementHigh=2)

    adsk = types.ModuleType("adsk")
    adsk.core = core
    adsk.fusion = fusion_mod

    global _app
    _app = _FakeApp()

    for name, module in (("adsk", adsk), ("adsk.core", core),
                         ("adsk.fusion", fusion_mod)):
        sys.modules[name] = module

    spec = importlib.util.spec_from_file_location("simkit_addin_under_test", ADDIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.run(None)
    yield module
    module.stop(None)

    for name in ("adsk", "adsk.core", "adsk.fusion"):
        sys.modules.pop(name, None)


def _request(payload: dict, timeout: float = 10.0) -> dict:
    with socket.create_connection(("127.0.0.1", 9720), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    return json.loads(b"".join(chunks).decode())


def test_addin_answers_ping(addin):
    response = _request({"command": "ping", "params": {}})
    assert response["ok"], response
    assert response["result"]["application"] == "Autodesk Fusion"
    # The stub has no design open, which the add-in should report rather than
    # pretend otherwise.
    assert response["result"]["is_design"] is False


def test_command_runs_off_the_socket_thread(addin):
    """The whole point of the custom event: commands must not run inline."""
    seen = {}

    def spy(_params):
        seen["thread"] = threading.current_thread().name
        return {"ok": True}

    addin.COMMANDS["_spy"] = spy
    try:
        response = _request({"command": "_spy", "params": {}})
        assert response["ok"], response
        assert "thread" in seen
        # It ran on the thread the fake app dispatched to, not the connection's.
        assert "Thread-" in seen["thread"]
    finally:
        addin.COMMANDS.pop("_spy", None)


def test_unknown_command_is_reported_not_swallowed(addin):
    response = _request({"command": "no_such_command", "params": {}})
    assert response["ok"] is False
    assert "unknown command" in response["error"].lower()
    assert "ping" in response["error"], "the error should list what is available"


def test_handler_exception_comes_back_as_an_error(addin):
    def boom(_params):
        raise ValueError("inner radius must be smaller")

    addin.COMMANDS["_boom"] = boom
    try:
        response = _request({"command": "_boom", "params": {}})
        assert response["ok"] is False
        assert "inner radius must be smaller" in response["error"]
        assert "traceback" in response
    finally:
        addin.COMMANDS.pop("_boom", None)


def test_millimetre_to_centimetre_conversion(addin):
    """Fusion's API is centimetres; everything SimKit exposes is millimetres."""
    assert addin._cm(10.0) == 1.0
    assert addin._mm(1.0) == 10.0
    assert addin._cm(addin._mm(3.7)) == pytest.approx(3.7)
