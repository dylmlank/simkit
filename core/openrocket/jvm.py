"""Starting OpenRocket's own Java engine in-process.

OpenRocket is a Java application with no command-line interface, so the only
way to get its real physics -- the same Barrowman aerodynamics and RK4
integrator the GUI uses -- is to load its jar into a JVM and call it. JPype
does that, and OpenRocket's bundled JRE is used rather than a system Java, so
this works on a machine with no JDK installed at all (which is the usual case).

Three details matter and each one fails loudly if skipped:

  * OpenRocket 23.09 renamed every package from `net.sf.openrocket` to
    `info.openrocket.core`; code written against the old names will not resolve.
  * The application is wired with Guice, so `Application.setInjector` has to be
    called before anything else touches a service.
  * `CoreModule.startLoader()` kicks off the motor-database load. Without it,
    opening any design that references a motor throws "startLoading() has not
    been called" from deep inside a Guice provider.

A JVM cannot be restarted inside a process, so the boot is done once and
cached.
"""
from __future__ import annotations

import threading

from .. import paths

_lock = threading.Lock()
_booted: dict | None = None


def boot() -> dict:
    """Start the JVM and wire OpenRocket up. Idempotent.

    Returns the handful of Java classes the rest of the connector needs.
    """
    global _booted
    with _lock:
        if _booted is not None:
            return _booted

        try:
            import jpype
            import jpype.imports  # noqa: F401  (registers the import hook)
        except ImportError:
            raise RuntimeError(
                "OpenRocket support needs JPype.\n"
                "  pip install jpype1"
            )

        found = paths.find_openrocket().require()
        jar = found.extra.get("jar", "")
        jvm = found.extra.get("jvm", "")
        if not jvm:
            raise RuntimeError(
                f"OpenRocket was found at {found.path} but it has no bundled "
                "JVM, and SimKit needs one to run simulations.\n"
                "  Point SIMKIT_OPENROCKET_JVM at a jvm.dll from any Java 17+ "
                "runtime."
            )

        if not jpype.isJVMStarted():
            # convertStrings so Java strings arrive as ordinary Python str.
            jpype.startJVM(jvmpath=jvm, classpath=[jar], convertStrings=True)

        from com.google.inject import Guice
        from info.openrocket.core.startup import Application, CoreModule
        from info.openrocket.core.plugin import PluginModule

        core = CoreModule()
        Application.setInjector(Guice.createInjector(core, PluginModule()))
        # Loads the bundled thrust-curve database in the background; every
        # design that names a motor depends on it.
        core.startLoader()

        from info.openrocket.core.document import OpenRocketDocument  # noqa: F401
        from info.openrocket.core.file import GeneralRocketLoader
        from info.openrocket.core.file import RocketSaver  # noqa: F401
        from info.openrocket.core.simulation import FlightDataType
        from java.io import File

        _booted = {
            "jpype": jpype,
            "version": found.version,
            "jar": jar,
            "File": File,
            "GeneralRocketLoader": GeneralRocketLoader,
            "FlightDataType": FlightDataType,
            "Application": Application,
        }
        return _booted


def is_available() -> tuple[bool, str]:
    """Whether a simulation could run, and why not if it could not."""
    try:
        import jpype  # noqa: F401
    except ImportError:
        return False, "JPype is not installed (pip install jpype1)"
    found = paths.find_openrocket()
    if not found.ok:
        return False, found.detail
    if not found.extra.get("jvm"):
        return False, "OpenRocket has no bundled JVM; set SIMKIT_OPENROCKET_JVM"
    return True, ""
