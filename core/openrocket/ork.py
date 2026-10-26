"""Reading and editing .ork design files directly.

A .ork is a zip holding `rocket.ork` (XML) plus any decal images. Editing that
XML needs no JVM, which matters for two reasons: parameter sweeps stay cheap,
and a design can be inspected on a machine where OpenRocket will not start.

Simulation still goes through OpenRocket itself (`sim.py`) -- this module only
changes the inputs.
"""
from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
import zipfile

import config

XML_ENTRY = "rocket.ork"

#: Element tags that are rocket components rather than metadata.
COMPONENT_TAGS = {
    "nosecone", "bodytube", "transition", "trapezoidfinset", "ellipticalfinset",
    "freeformfinset", "tubefinset", "launchlug", "railbutton", "innertube",
    "tubecoupler", "engineblock", "centeringring", "bulkhead", "masscomponent",
    "parachute", "streamer", "shockcord", "stage", "podset", "boosterset",
    "rocket",
}

#: Numeric fields worth exposing for editing, per what they mean physically.
EDITABLE = {
    "length", "thickness", "aftradius", "forcradius", "foreradius",
    "outerradius", "innerradius", "radius", "rootchord", "tipchord",
    "sweeplength", "height", "fincount", "cantangle", "mass", "shapeparameter",
    "aftshoulderradius", "aftshoulderlength", "foreshoulderradius",
    "foreshoulderlength", "packedradius", "packedlength", "cd", "diameter",
    "linelength", "linecount", "overhang", "axialoffset",
}


def resolve(path: str) -> str:
    if os.path.isabs(path):
        candidate = path
    else:
        candidate = os.path.join(config.out_dir(), os.path.basename(path))
    if not candidate.lower().endswith(".ork"):
        candidate += ".ork"
    if os.path.isfile(candidate):
        return candidate
    if os.path.isfile(path):
        return os.path.abspath(path)
    raise FileNotFoundError(f"no OpenRocket design at {candidate}")


def read_xml(path: str) -> ET.ElementTree:
    with zipfile.ZipFile(resolve(path)) as zf:
        with zf.open(XML_ENTRY) as fh:
            return ET.parse(fh)


def write_xml(source: str, tree: ET.ElementTree, out_path: str) -> str:
    """Rewrite a .ork with new XML, preserving decals and any other entries."""
    source = resolve(source)
    out_path = os.path.abspath(out_path)
    if not out_path.lower().endswith(".ork"):
        out_path += ".ork"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    xml_bytes = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    # Write to a temporary file first so an interrupted save cannot leave a
    # corrupt design behind, and so source and target may be the same file.
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.writestr(XML_ENTRY, xml_bytes)
        for item in src.infolist():
            if item.filename != XML_ENTRY:
                dst.writestr(item, src.read(item.filename))
    shutil.move(tmp, out_path)
    return out_path


def _components(root: ET.Element):
    """Yield (element, name, path) for every component, depth-first."""
    def walk(node, trail):
        for child in node:
            if child.tag in COMPONENT_TAGS:
                name_el = child.find("name")
                name = name_el.text if name_el is not None else child.tag
                here = trail + [name]
                yield child, name, " / ".join(here)
                yield from walk(child, here)
            else:
                yield from walk(child, trail)

    name_el = root.find("./rocket/name")
    root_name = name_el.text if name_el is not None else "Rocket"
    rocket = root.find("rocket")
    if rocket is not None:
        yield from walk(rocket, [root_name])


def list_parameters(path: str) -> dict:
    """Every component and the numeric fields that can be changed on it."""
    tree = read_xml(path)
    out = []
    for element, name, trail in _components(tree.getroot()):
        fields = {}
        for child in element:
            if child.tag in EDITABLE and child.text:
                try:
                    fields[child.tag] = float(child.text)
                except ValueError:
                    fields[child.tag] = child.text
        if fields:
            out.append({"component": name, "type": element.tag,
                        "path": trail, "fields": fields})
    return {"file": resolve(path), "components": out, "count": len(out)}


def set_parameters(path: str, component: str, changes: dict,
                   out_path: str = "") -> dict:
    """Change numeric fields on a named component.

    Matching is by component name, case-insensitively. All units are SI, as
    they are in the file itself -- lengths in metres, angles in radians -- so a
    body tube 30 cm long is `length: 0.3`, not 300.
    """
    src = resolve(path)
    tree = read_xml(src)
    wanted = component.strip().lower()

    matches = [(el, name) for el, name, _ in _components(tree.getroot())
               if name and name.strip().lower() == wanted]
    if not matches:
        names = sorted({n for _, n, _ in _components(tree.getroot()) if n})
        raise KeyError(
            f"no component named {component!r}. Present: {', '.join(names)}")
    if len(matches) > 1:
        raise KeyError(
            f"{len(matches)} components are named {component!r}; rename them in "
            "OpenRocket so the target is unambiguous.")

    element, name = matches[0]
    applied, rejected = {}, {}
    for field, value in changes.items():
        field = field.lower()
        if field not in EDITABLE:
            rejected[field] = "not an editable field"
            continue
        child = element.find(field)
        if child is None:
            child = ET.SubElement(element, field)
        before = child.text
        child.text = repr(float(value)) if isinstance(value, (int, float)) else str(value)
        applied[field] = {"from": before, "to": child.text}

    if not applied:
        raise ValueError(
            f"nothing was changed on {name}. Rejected: {rejected}. "
            f"Editable fields are: {', '.join(sorted(EDITABLE))}")

    target = out_path or src
    if target and not os.path.isabs(target):
        target = os.path.join(config.out_dir(), os.path.basename(target))
    written = write_xml(src, tree, target)
    return {"file": written, "component": name, "applied": applied,
            "rejected": rejected}


def sweep(path: str, component: str, field: str, values: list,
          prefix: str = "") -> dict:
    """Produce one design variant per value, ready to simulate.

    This is how a question like "what does fin span do to stability" gets
    answered: build the variants here, run them through `sim.simulate`, and
    compare. Each variant is a real file, so anything can be opened in
    OpenRocket afterwards to check it by eye.
    """
    src = resolve(path)
    stem = prefix or os.path.splitext(os.path.basename(src))[0]
    made = []
    for value in values:
        target = os.path.join(
            config.out_dir(), f"{stem}_{field}_{value:g}".replace(".", "p") + ".ork")
        result = set_parameters(src, component, {field: value}, target)
        made.append({"value": float(value), "file": result["file"]})
    return {"source": src, "component": component, "field": field,
            "variants": made, "count": len(made)}


def extract_example(name: str = "A simple model rocket", out_name: str = "") -> dict:
    """Copy one of OpenRocket's bundled example designs into the output dir.

    Useful as a starting point, and as a way to check the whole connector works
    without needing the user to supply a design first.
    """
    from .. import paths

    jar = paths.find_openrocket().require().extra.get("jar", "")
    if not jar:
        raise RuntimeError("no OpenRocket jar to read examples from")
    with zipfile.ZipFile(jar) as zf:
        examples = [n for n in zf.namelist()
                    if n.startswith("datafiles/examples/") and n.endswith(".ork")]
        wanted = name.lower().removesuffix(".ork")
        hit = next((n for n in examples
                    if os.path.basename(n).lower().removesuffix(".ork") == wanted), "")
        if not hit:
            available = [os.path.basename(n)[:-4] for n in examples]
            raise KeyError(
                f"no bundled example called {name!r}. Available: "
                + "; ".join(sorted(available)))
        target = os.path.join(
            config.out_dir(), (out_name or os.path.basename(hit)))
        if not target.lower().endswith(".ork"):
            target += ".ork"
        with zf.open(hit) as fh, open(target, "wb") as out:
            shutil.copyfileobj(fh, out)
    return {"file": target, "example": os.path.basename(hit)}


def list_examples() -> dict:
    from .. import paths

    jar = paths.find_openrocket().require().extra.get("jar", "")
    with zipfile.ZipFile(jar) as zf:
        names = sorted(os.path.basename(n)[:-4] for n in zf.namelist()
                       if n.startswith("datafiles/examples/") and n.endswith(".ork"))
    return {"examples": names, "count": len(names)}
