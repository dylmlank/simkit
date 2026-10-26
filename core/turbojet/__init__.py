"""Turbojet: design a homebuilt engine around a turbocharger core, then build it.

Physics comes from MechKit (run in its own interpreter, see `bridge.py`);
geometry from SimKit's FreeCAD connector.
"""

from . import bridge, cores, engine, flowtest  # noqa: F401
