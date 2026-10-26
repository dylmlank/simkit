"""FreeCAD geometry, checked against known-good numbers.

These drive the real geometry kernel through freecadcmd, so each test starts a
process and they are not fast. They are worth it: the arithmetic is what the
whole CAD side rests on, and a wrong unit or a silently failed boolean would
otherwise only show up in a CFD result days later.
"""
from __future__ import annotations

import math
import os

import pytest

from core import paths
from core.freecad import ops, runner

FOUND = paths.find_freecad()
needs_freecad = pytest.mark.skipif(not FOUND.ok, reason="FreeCAD is not installed")

pytestmark = needs_freecad


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    import config

    config.OUT_DIR = str(tmp_path_factory.mktemp("freecad"))
    ops.new_document("t")
    return "t"


def test_generated_literals_are_python_not_json():
    """json.dumps writes `false`, which is a NameError in generated code."""
    assert ops._lit(False) == "False"
    assert ops._lit(None) == "None"
    assert ops._lit(True) == "True"
    # Windows paths must survive intact.
    assert ops._lit(r"E:\out\a.FCStd") == r"'E:\\out\\a.FCStd'"


def test_box_volume_and_centroid(doc):
    result = ops.add_primitive(doc, "box",
                               {"length": 20, "width": 10, "height": 5}, "B")
    assert result["volume_mm3"] == pytest.approx(1000.0)
    assert result["area_mm2"] == pytest.approx(2 * (200 + 100 + 50))
    assert result["center_of_mass_mm"] == pytest.approx([10, 5, 2.5])
    assert result["solids"] == 1 and result["valid"]


def test_cylinder_matches_pi_r_squared_h(doc):
    result = ops.add_primitive(doc, "cylinder", {"radius": 10, "height": 50}, "C")
    assert result["volume_mm3"] == pytest.approx(math.pi * 100 * 50, rel=1e-6)


def test_tube_is_the_difference_of_two_cylinders(doc):
    ro, ri, h = 25.0, 23.0, 300.0
    result = ops.add_tube(doc, {"outer_radius": ro, "inner_radius": ri,
                                "height": h}, "T")
    expected = math.pi * (ro ** 2 - ri ** 2) * h
    assert result["volume_mm3"] == pytest.approx(expected, rel=1e-6)


def test_tube_rejects_impossible_walls(doc):
    with pytest.raises(ValueError, match="smaller than outer_radius"):
        ops.add_tube(doc, {"outer_radius": 10, "inner_radius": 12, "height": 5})


def test_cut_removes_the_overlap(doc):
    ops.new_document("cut")
    ops.add_primitive("cut", "box", {"length": 20, "width": 20, "height": 20}, "A")
    ops.add_primitive("cut", "box", {"length": 10, "width": 10, "height": 10}, "B")
    result = ops.boolean("cut", "cut", "A", "B", name="R")
    # B sits in A's corner, so exactly its own volume is removed.
    assert result["volume_mm3"] == pytest.approx(8000 - 1000)


def test_non_overlapping_cut_is_an_error_not_an_empty_body(doc):
    ops.new_document("nocut")
    ops.add_primitive("nocut", "box", {"length": 5, "width": 5, "height": 5}, "A")
    ops.add_primitive("nocut", "box", {"length": 5, "width": 5, "height": 5}, "B",
                      position=[100, 100, 100])
    with pytest.raises(RuntimeError, match="do not overlap"):
        ops.boolean("nocut", "intersect", "A", "B")


def test_union_of_touching_boxes_is_their_sum(doc):
    ops.new_document("uni")
    ops.add_primitive("uni", "box", {"length": 10, "width": 10, "height": 10}, "A")
    ops.add_primitive("uni", "box", {"length": 10, "width": 10, "height": 10}, "B",
                      position=[10, 0, 0])
    result = ops.boolean("uni", "union", "A", "B")
    assert result["volume_mm3"] == pytest.approx(2000.0)


def test_revolved_cone_matches_the_closed_form(doc):
    """A right triangle revolved about Z is a cone: V = pi r^2 h / 3."""
    ops.new_document("rev")
    r, h = 20.0, 60.0
    result = ops.revolve_profile("rev", [[0, 0], [r, 0], [0, h]], name="Cone")
    assert result["volume_mm3"] == pytest.approx(math.pi * r ** 2 * h / 3, rel=1e-3)


def test_extruded_square_is_a_prism(doc):
    ops.new_document("ext")
    result = ops.extrude_profile("ext", [[0, 0], [10, 0], [10, 10], [0, 10]], 7)
    assert result["volume_mm3"] == pytest.approx(700.0)


def test_missing_dimensions_are_named(doc):
    with pytest.raises(ValueError, match="missing width"):
        ops.add_primitive(doc, "box", {"length": 1, "height": 1})


def test_unknown_primitive_lists_the_options(doc):
    with pytest.raises(ValueError, match="Available"):
        ops.add_primitive(doc, "dodecahedron", {})


def test_missing_object_lists_what_is_present(doc):
    with pytest.raises(RuntimeError, match="Present"):
        ops.measure(doc, "NoSuchThing")


def test_stl_export_produces_a_readable_mesh(doc, tmp_path):
    ops.new_document("exp")
    ops.add_primitive("exp", "sphere", {"radius": 25}, "S")
    result = ops.export("exp", fmt="stl", target="S")
    assert result["facets"] > 100
    assert os.path.getsize(result["path"]) > 1000
    with open(result["path"], "rb") as fh:
        assert fh.read(200), "the STL should not be empty"


def test_step_export_round_trips_through_import(doc):
    ops.new_document("rt")
    ops.add_primitive("rt", "box", {"length": 30, "width": 20, "height": 10}, "Src")
    step = ops.export("rt", fmt="step", target="Src")

    ops.new_document("rt2")
    ops.import_file("rt2", step["path"])
    info = ops.document_info("rt2")
    assert info["object_count"] >= 1
    volumes = [o.get("volume_mm3") for o in info["objects"] if o.get("volume_mm3")]
    assert any(v == pytest.approx(6000.0, rel=1e-6) for v in volumes), volumes


def test_unknown_export_format_is_rejected(doc):
    with pytest.raises(ValueError, match="unknown format"):
        ops.export(doc, fmt="dwg")


def test_script_errors_surface_with_their_message():
    with pytest.raises(RuntimeError, match="deliberate"):
        runner.run("raise ValueError('deliberate')")


def test_script_can_return_values():
    result = runner.run("_result['answer'] = 6 * 7")
    assert result["answer"] == 42
