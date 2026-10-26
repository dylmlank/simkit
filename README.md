# SimKit

Connects Claude to four real engineering applications:

| Application | How it connects | Needs the app open? |
|---|---|---|
| **Autodesk Fusion 360** | Add-in inside Fusion, loopback socket | **Yes** |
| **FreeCAD** | `freecadcmd.exe`, headless | No |
| **OpenRocket** | Its own bundled JRE + jar, via JPype | No |
| **OpenFOAM** | WSL | No |

These drive the actual programs on real files. FreeCAD really opens `.FCStd`
documents, OpenRocket runs its own Barrowman/RK4 flight engine, and OpenFOAM
meshes and solves genuine cases. Nothing here re-implements the physics.

## What it can do

The interesting part is that the four compose. A shape modelled in CAD can be
exported to STL, cut into a flow domain, solved, and its drag read back:

```
freecad_add_solid   sphere, radius 50 mm
freecad_export      -> sphere.stl
openfoam_create_case  15 m/s, geometry_stl=sphere.stl, force_coeffs=true
openfoam_mesh       blockMesh + snappyHexMesh + checkMesh
openfoam_solve
openfoam_force_coefficients
```

That exact sequence returns **Cd = 0.462** for a sphere at Re = 1x10^5, against
a textbook value of 0.4-0.5, with lift at 0.002 (zero by symmetry, as it should
be). It is the standing validation case for the CAD-to-CFD path.

**39 tools**, grouped by application:

- **FreeCAD (11)** — documents, solid primitives and tubes, extrude and revolve
  profiles, booleans, fillets, mass properties, STEP/IGES/BREP/STL/OBJ import
  and export, and an arbitrary-Python escape hatch.
- **Fusion (13)** — solids, booleans, fillets, **user parameters** (the reason
  to use Fusion at all), body listing, exports, viewport screenshots, and an
  escape hatch that runs on Fusion's main thread.
- **OpenRocket (7)** — read a design's component tree, run real simulations,
  compare every saved simulation, edit component dimensions, and sweep one
  dimension across a range to see what it does to the flight.
- **OpenFOAM (7)** — write a complete case, mesh it, solve it, read force
  coefficients, post-process, and convert to VTK for ParaView.
- **Turbojet (5)** — design a homebuilt engine around a turbocharger core, build
  the parts you fabricate, and flow-test the nozzle. See below.
- **survey (1)** — what is installed, what version, and what is reachable now.

## Building a turbojet

The one workflow that uses everything at once. It needs
[MechKit](../mechkit) for the physics — SimKit does not re-implement the cycle.

```
turbojet_cores                          what you can build around
turbojet_design    core=GT2560R         close the design, name the binding constraint
turbojet_build     core=GT2560R         generate the parts you fabricate
turbojet_flow_test core=GT2560R         CFD the nozzle, compare with the 1-D cycle
turbojet_size_nozzle core=GT2560R       iterate the exit until CFD matches the cycle
```

**The design runs backwards on purpose.** The textbook problem picks a thrust
and derives a wheel; ask MechKit for 60 N directly and it demands a 48 mm wheel
at 160,000 rpm with a burst margin of 1.22 — a wheel you cannot buy and should
not make. So SimKit starts from the core you own and reports the thrust that
follows:

| Core | Max thrust | rpm | Binding constraint |
|---|---|---|---|
| GT1544 | 42 N | 171k | inducer choking, Mach 0.90 |
| TD04 | 51 N | 157k | inducer choking, Mach 0.91 |
| GT2560R | 61 N | 143k | inducer choking, Mach 0.90 |
| T3 60-trim | 103 N | 124k | inducer choking, Mach 0.98 |

Every one runs out of *air* before it runs out of *wheel*, which is the
reassuring failure mode.

**The turbo gives you the wheels; SimKit gives you everything else** — flame
tube with its air-admission holes placed from the combustor's own air splits,
casing, diffuser vanes, nozzle guide vanes, exhaust nozzle. All as real CAD,
exportable to STEP or STL.

**Two safety notes the tools will repeat at you.** The core table is *nominal*;
wheels vary between variants of one turbo family, so measure yours and pass
`measured_compressor_od_mm` and friends. And at 95% of an automotive speed
rating the compressor burst margin falls to about 1.26, which raises a
containment warning — an automotive turbo has a wastegate limiting boost, a jet
has nothing. The thrust curve is flat up there anyway; run it at 80-85%.

**Flow testing.** The nozzle exit area sets the engine's operating point, so it
is the part worth solving properly. It is a body of revolution, so it runs as
an axisymmetric wedge — cheaper and more accurate than a coarse 3-D mesh. For
the GT2560R design, CFD passes 0.1590 kg/s against the cycle's assumed 0.1475
(discharge coefficient 0.90), so the 1-D sizing is about 8% optimistic and
`turbojet_size_nozzle` corrects it.

The compressor and turbine wheels are *not* flow-tested, and cannot be: they
come with the turbocharger and SimKit has no geometry for them. `write_mrf()`
exists for the day you scan one.

## Install

```bash
pip install -r requirements.txt
```

Only `jpype1` (OpenRocket) and `anthropic`/`mcp` are needed; the other three
connectors talk to their applications directly.

### As an MCP server

```json
{
  "mcpServers": {
    "simkit": {
      "command": "C:\\Users\\dylan\\.local\\bin\\uv.exe",
      "args": ["run", "--python", "3.12", "--with", "mcp", "--with", "jpype1",
               "python", "E:\\simkit\\mcp_server.py"]
    }
  }
}
```

Works with both MCP SDK 1.x and 2.0 — the 2.0 release renamed `FastMCP` to
`MCPServer`, and `mcp_server.py` imports whichever is present.

### As a standalone agent

```bash
uv run --python 3.12 --with anthropic --with jpype1 python agent.py "Model a 50 mm sphere in FreeCAD and find its drag coefficient at 15 m/s"
```

Reads `ANTHROPIC_API_KEY` from the environment or a `.env` beside `agent.py`.

## Per-application setup

### Fusion 360 — the only one needing manual steps

Fusion has no headless mode and no external API, so SimKit has to run *inside*
it. Install the add-in:

```
fusion_install_addin
```

Then, in Fusion: **Utilities > Add-Ins > Add-Ins tab > SimKit > Run**, and tick
*Run on Startup*. There is no API for that last step — Fusion only lets a user
enable an add-in.

Fusion must then be open with a design document for any `fusion_*` tool to
work. `fusion_status` says whether it is reachable.

### FreeCAD, OpenRocket, OpenFOAM

Nothing to do if they are installed in their usual locations. Override
discovery only if needed:

| Variable | Purpose |
|---|---|
| `SIMKIT_FREECAD` | path to `freecadcmd.exe` |
| `SIMKIT_OPENROCKET` | OpenRocket install folder |
| `SIMKIT_OPENROCKET_JVM` | a `jvm.dll`, if not using the bundled JRE |
| `SIMKIT_WSL_DISTRO` | which WSL distro holds OpenFOAM |
| `SIMKIT_OPENFOAM_BASHRC` | which OpenFOAM install to use |
| `SIMKIT_OUT` | where generated files land (default `./out`) |

## Two things that will bite you

**OpenFOAM comes in two incompatible forks.** The Foundation build
(openfoam.org, versions like `13`) and the ESI build (openfoam.com, versions
like `v2412`) share a name and a mesh format but disagree about what the case
files are called:

| | Foundation | ESI |
|---|---|---|
| viscosity | `constant/physicalProperties` | `constant/transportProperties` |
| turbulence | `constant/momentumTransport` | `constant/turbulenceProperties` |
| model key | `model kEpsilon;` | `RASModel kEpsilon;` |
| controlDict | `solver incompressibleFluid;` | `application simpleFoam;` |
| snappy seed | `insidePoint` | `locationInMesh` |
| run command | `foamRun` | `simpleFoam` / `pimpleFoam` |

SimKit detects which one it is talking to (by whether `foamRun` exists) and
writes the matching dialect. This machine has **both** installed, which is how
the difference was found: the wrong seed-point keyword does not raise, it
silently deletes the entire mesh and fails several minutes later with an error
that names no keyword at all.

**Units change at the CAD/CFD boundary.** CAD tools are millimetres; OpenRocket
and OpenFOAM are SI. `openfoam_create_case` takes `stl_scale`, defaulting to
`0.001`, to convert. Left at 1.0, a 50 mm part enters the domain as a 50 m one,
meshes without complaint, and returns drag figures that mean nothing.

## Layout

```
config.py                 settings and environment overrides
core/
  paths.py                finds the four applications; detects the OpenFOAM fork
  tools.py                the 39 tools: schemas + dispatch, single source of truth
  freecad/                runner.py (headless protocol), ops.py
  fusion/                 bridge.py (socket client), ops.py
  openrocket/             jvm.py (JPype bootstrap), sim.py, ork.py (.ork editing)
  openfoam/               dialect.py, case.py (case writer), run.py, wsl.py
fusion_addin/SimKit/      the add-in that runs inside Fusion
mcp_server.py             MCP surface
agent.py                  standalone agent surface
tests/                    143 tests
```

`core/tools.py` is the single source of truth: a capability is defined once as
a handler plus a `TOOLS` entry, and both surfaces pick it up. An assertion at
import time fails the build if the two ever disagree.

## Tests

```bash
python -m pytest
```

143 tests. They exercise the real applications where possible: FreeCAD geometry
is checked against closed-form volumes, OpenRocket runs actual simulations, and
OpenFOAM case generation is verified in both dialects. Tests skip cleanly when
an application is absent.

The Fusion add-in is tested against a stub of Fusion's API — including a check
that commands really are handed to the main thread rather than run on the
socket thread, which is the one mistake that corrupts documents instead of
raising.

`pytest.ini` disables pytest's faulthandler. That is deliberate and explained in
the file: the JVM uses guard-page access violations as part of normal
operation, and faulthandler reports them as fatal crashes when they are not.
