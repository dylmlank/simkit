"""OpenRocket design reading, editing and simulation.

The .ork tests need no JVM and always run. The simulation tests need JPype and
OpenRocket's bundled JRE, and skip cleanly when either is missing.
"""
from __future__ import annotations

import os

import pytest

from core.openrocket import jvm, ork

AVAILABLE, WHY = jvm.is_available()
needs_jvm = pytest.mark.skipif(not AVAILABLE, reason=f"OpenRocket engine: {WHY}")


@pytest.fixture(scope="module")
def design(tmp_path_factory):
    """A real design, taken from the examples OpenRocket ships."""
    out = tmp_path_factory.mktemp("ork")
    target = str(out / "demo.ork")
    result = ork.extract_example("A simple model rocket", target)
    return result["file"]


# --- file handling, no JVM --------------------------------------------------

def test_examples_are_listed():
    result = ork.list_examples()
    assert result["count"] > 0
    assert "A simple model rocket" in result["examples"]


def test_unknown_example_names_the_alternatives():
    with pytest.raises(KeyError, match="Available"):
        ork.extract_example("Not A Real Design")


def test_parameters_are_read(design):
    result = ork.list_parameters(design)
    names = {c["component"] for c in result["components"]}
    assert {"Nose cone", "Body tube"} <= names
    tube = next(c for c in result["components"] if c["component"] == "Body tube")
    assert tube["fields"]["length"] == pytest.approx(0.3)


def test_editing_preserves_the_other_zip_entries(design, tmp_path):
    """A .ork carries decal images alongside the XML; losing them corrupts it."""
    import zipfile

    with zipfile.ZipFile(design) as zf:
        before = set(zf.namelist())
    assert any(n.startswith("decals/") for n in before), "fixture should have decals"

    target = str(tmp_path / "edited.ork")
    ork.set_parameters(design, "Body tube", {"length": 0.42}, target)
    with zipfile.ZipFile(target) as zf:
        after = set(zf.namelist())
    assert before == after


def test_edit_round_trips(design, tmp_path):
    target = str(tmp_path / "edited.ork")
    result = ork.set_parameters(design, "Body tube", {"length": 0.42}, target)
    assert result["applied"]["length"]["to"] == "0.42"
    reread = ork.list_parameters(target)
    tube = next(c for c in reread["components"] if c["component"] == "Body tube")
    assert tube["fields"]["length"] == pytest.approx(0.42)


def test_unknown_component_lists_what_exists(design):
    with pytest.raises(KeyError, match="Present:"):
        ork.set_parameters(design, "Warp Nacelle", {"length": 1.0})


def test_non_editable_field_is_refused(design, tmp_path):
    with pytest.raises(ValueError, match="nothing was changed"):
        ork.set_parameters(design, "Body tube", {"colour": 3},
                           str(tmp_path / "x.ork"))


def test_sweep_makes_one_file_per_value(design, tmp_path):
    import config

    config.OUT_DIR = str(tmp_path)
    result = ork.sweep(design, "Body tube", "length", [0.2, 0.3, 0.4])
    assert result["count"] == 3
    assert all(os.path.isfile(v["file"]) for v in result["variants"])
    assert len({v["file"] for v in result["variants"]}) == 3


# --- the real engine --------------------------------------------------------

@needs_jvm
def test_design_is_read_through_openrockets_own_loader(design):
    from core.openrocket import sim

    info = sim.design_info(design)
    assert info["name"] == "A simple model rocket"
    assert info["stages"] == 1
    assert info["component_count"] > 5
    assert len(info["simulations"]) > 0


@needs_jvm
def test_simulation_produces_a_physical_flight(design):
    from core.openrocket import sim

    result = sim.simulate(design, 0)
    # A small model rocket on an A-class motor: tens of metres, subsonic.
    assert 10 < result["apogee_m"] < 300
    assert 0 < result["max_velocity_m_s"] < 200
    assert result["max_mach"] < 1.0
    assert result["time_to_apogee_s"] < result["flight_time_s"]
    assert result["ground_hit_velocity_m_s"] > 0


@needs_jvm
def test_longer_heavier_airframe_flies_lower(design, tmp_path):
    """Mass and drag both rise with body length, so apogee must fall."""
    import config
    from core.openrocket import sim

    config.OUT_DIR = str(tmp_path)
    apogees = []
    for length in (0.20, 0.30, 0.40):
        variant = str(tmp_path / f"v{length}.ork")
        ork.set_parameters(design, "Body tube", {"length": length}, variant)
        apogees.append(sim.simulate(variant, 0)["apogee_m"])

    assert apogees == sorted(apogees, reverse=True), (
        f"apogee should fall as the airframe grows, got {apogees}")


@needs_jvm
def test_series_is_downsampled_for_readability(design):
    from core.openrocket import sim

    result = sim.simulate(design, 0, series=True, samples=25)
    series = result["series"]
    assert "altitude" in series and "time" in series
    assert len(series["altitude"]) <= 25
    assert max(series["altitude"]) == pytest.approx(result["apogee_m"], rel=0.05)


@needs_jvm
def test_missing_simulation_index_is_reported(design):
    from core.openrocket import sim

    with pytest.raises(IndexError, match="does not exist"):
        sim.simulate(design, 999)
