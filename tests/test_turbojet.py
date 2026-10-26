"""Turbojet design, geometry and the compressible flow test.

The turbo-core arithmetic and the compressible case writer run everywhere. The
design closure needs MechKit and skips without it; nothing here starts a solver.
"""
from __future__ import annotations

import math
import os

import pytest

from core.openfoam import compressible
from core.openfoam.dialect import ESI, FOUNDATION, Dialect
from core.turbojet import bridge, cores, engine, flowtest

HAVE_MECHKIT, WHY = bridge.available()
needs_mechkit = pytest.mark.skipif(not HAVE_MECHKIT, reason=WHY)

FOUND = Dialect(FOUNDATION, "13")
ESI_D = Dialect(ESI, "v2412")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- cores ------------------------------------------------------------------

def test_tip_speed_follows_diameter_and_speed():
    core = cores.get("TD04")
    # v = pi D N / 60
    expected = math.pi * (core.compressor_od_mm / 1000) * core.max_rpm / 60
    assert core.tip_speed() == pytest.approx(expected)


def test_nominal_cores_say_they_are_nominal():
    """A table entry must never be mistaken for a measurement."""
    warnings = cores.get("GT2560R").check()
    assert any("nominal" in w for w in warnings)


def test_measured_core_does_not_carry_that_warning():
    core = cores.from_measurements("mine", 53.0, 36.0, 47.0, 150_000)
    assert core.measured
    assert not any("nominal" in w for w in core.check())


def test_swapped_inducer_and_exducer_is_caught():
    """The inducer is the small inlet eye; measuring the wrong one is easy."""
    core = cores.from_measurements("swapped", 36.0, 53.0, 47.0, 150_000)
    assert any("swapped" in w for w in core.check())


def test_unknown_core_lists_the_known_ones():
    with pytest.raises(KeyError, match="Known:"):
        cores.get("RB211")


# --- design -----------------------------------------------------------------

@needs_mechkit
def test_design_closes_and_balances_power():
    d = engine.design_at_rpm("GT2560R", 140_000)
    assert d["thrust_N"] > 0
    assert 1.5 < d["pressure_ratio"] < 6
    balance = d["power_balance"]
    assert balance["closed"], balance
    assert balance["compressor_absorbs_kW"] == pytest.approx(
        balance["turbine_delivers_kW"], rel=0.05)


@needs_mechkit
def test_overspeeding_a_core_is_refused():
    core = cores.get("GT2560R")
    with pytest.raises(ValueError, match="containment limit"):
        engine.design_at_rpm(core, core.max_rpm * 1.2)


@needs_mechkit
def test_faster_wheel_makes_more_thrust():
    low = engine.design_at_rpm("GT2560R", 110_000)["thrust_N"]
    high = engine.design_at_rpm("GT2560R", 140_000)["thrust_N"]
    assert high > low


@needs_mechkit
def test_bigger_core_makes_more_thrust():
    small = engine.max_thrust("GT1544")["best"]["thrust_N"]
    large = engine.max_thrust("T3 60-trim")["best"]["thrust_N"]
    assert large > small


@needs_mechkit
def test_design_reports_the_binding_constraint():
    d = engine.max_thrust("GT2560R")["best"]
    assert d["binding_constraint"]
    # Every core in this class runs out of air before it runs out of wheel.
    assert "choking" in d["binding_constraint"] or "rating" in d["binding_constraint"]


@needs_mechkit
def test_design_carries_station_conditions_for_the_flow_test():
    d = engine.design_at_rpm("GT2560R", 140_000)
    total_p, total_t = flowtest.nozzle_inlet_conditions(d)
    assert total_p > 101325, "turbine exit must be above ambient to blow down"
    assert 600 < total_t < 1200


# --- nozzle geometry --------------------------------------------------------

@needs_mechkit
def test_nozzle_converges_from_turbine_exit():
    d = engine.design_at_rpm("GT2560R", 140_000)
    stations = flowtest.nozzle_stations(d)
    assert len(stations) >= 2
    assert stations[-1][1] < stations[0][1], "a nozzle must converge"
    assert stations[-1][0] > stations[0][0]


# --- compressible physics ---------------------------------------------------

def test_choked_mass_flow_matches_the_closed_form():
    # 2 bar, 900 K, 1000 mm2 throat.
    m = compressible.choked_mass_flow(200_000.0, 900.0, 1000e-6)
    gamma, R = 1.4, compressible.gas_constant()
    expected = (1000e-6 * 200_000.0 * math.sqrt(gamma / R)
                * (2 / (gamma + 1)) ** ((gamma + 1) / (2 * (gamma - 1)))
                / math.sqrt(900.0))
    assert m == pytest.approx(expected)


def test_choking_pressure_ratio_is_the_textbook_value():
    assert compressible.pressure_ratio_to_choke() == pytest.approx(1.8929, abs=1e-3)


def test_choked_flow_scales_with_pressure_and_area():
    base = compressible.choked_mass_flow(200_000.0, 900.0, 1000e-6)
    assert compressible.choked_mass_flow(400_000.0, 900.0, 1000e-6) == \
        pytest.approx(2 * base)
    assert compressible.choked_mass_flow(200_000.0, 900.0, 2000e-6) == \
        pytest.approx(2 * base)


# --- wedge mesh -------------------------------------------------------------

def test_wedge_collapses_the_axis_edge(tmp_path):
    """Repeating the axis index is what makes blockMesh build a prism.

    Distinct indices at the same point produce zero-area faces instead, which
    checkMesh reports as infinite skewness.
    """
    compressible.write_wedge_mesh(
        str(tmp_path), [(0, 25), (40, 15)], dia=ESI_D)
    text = read(os.path.join(str(tmp_path), "system", "blockMeshDict"))
    block = next(line for line in text.splitlines() if "hex (" in line)
    indices = block.split("(")[1].split(")")[0].split()
    assert indices[0] == indices[4], "axis vertex must repeat"
    assert indices[1] == indices[5], "axis vertex must repeat"


def test_wedge_declares_both_wedge_patches(tmp_path):
    compressible.write_wedge_mesh(str(tmp_path), [(0, 25), (40, 15)], dia=ESI_D)
    text = read(os.path.join(str(tmp_path), "system", "blockMeshDict"))
    assert text.count("type            wedge;") == 2
    assert "name            axis;" in text


def test_wedge_rejects_stations_that_do_not_advance(tmp_path):
    with pytest.raises(ValueError, match="advance along the axis"):
        compressible.write_wedge_mesh(
            str(tmp_path), [(0, 25), (0, 15)], dia=ESI_D)


def test_wedge_needs_two_stations(tmp_path):
    with pytest.raises(ValueError, match="at least two"):
        compressible.write_wedge_mesh(str(tmp_path), [(0, 25)], dia=ESI_D)


# --- compressible case ------------------------------------------------------

@pytest.mark.parametrize("dia,name", [(FOUND, "physicalProperties"),
                                      (ESI_D, "thermophysicalProperties")])
def test_thermo_dict_written_in_the_right_dialect(tmp_path, dia, name):
    path = compressible.write_thermo(str(tmp_path), dia)
    assert os.path.basename(path) == name
    assert "perfectGas" in read(path)


def test_compressible_solver_per_fork():
    assert FOUND.compressible_solver(True) == "fluid"
    assert ESI_D.compressible_solver(True) == "rhoSimpleFoam"
    assert ESI_D.compressible_solver(False) == "rhoPimpleFoam"
    assert FOUND.run_command(True, compressible=True) == "foamRun"
    assert ESI_D.run_command(True, compressible=True) == "rhoSimpleFoam"


def test_pressure_is_absolute_not_kinematic(tmp_path):
    """Compressible p is in Pa; the incompressible solver uses p/rho."""
    compressible.write_fields(str(tmp_path), 200_000.0, 900.0, 101325.0)
    text = read(os.path.join(str(tmp_path), "0", "p"))
    assert "[1 -1 -2 0 0 0 0]" in text, "p must carry pressure dimensions"
    assert "totalPressure" in text


def test_total_temperature_inlet_carries_gamma(tmp_path):
    """Without gamma the condition cannot convert total to static and errors."""
    compressible.write_fields(str(tmp_path), 200_000.0, 900.0, 101325.0)
    text = read(os.path.join(str(tmp_path), "0", "T"))
    assert "inletOutletTotalTemperature" in text
    assert "gamma" in text


def test_compressible_case_needs_a_pressure_drop(tmp_path):
    with pytest.raises(ValueError, match="not above"):
        compressible.create_nozzle_case(
            "bad_nozzle", [(0, 25), (40, 15)],
            total_pressure=90_000.0, total_temperature=900.0,
            back_pressure=101_325.0)


# --- solver robustness ------------------------------------------------------

@pytest.mark.parametrize("dia,name", [(FOUND, "fvConstraints"),
                                      (ESI_D, "fvOptions")])
def test_limiters_written_in_the_right_dialect(tmp_path, dia, name):
    path = compressible.write_constraints(str(tmp_path), dia=dia)
    assert os.path.basename(path) == name
    text = read(path)
    assert "limitTemperature" in text


def test_temperature_limiter_brackets_the_inlet_condition(tmp_path):
    """Wide enough not to touch the answer, tight enough to catch divergence."""
    text = read(compressible.write_constraints(str(tmp_path), 150.0, 2500.0))
    assert "min             150" in text
    assert "max             2500" in text


def test_pressure_is_bounded_during_the_transient(tmp_path):
    """A steady compressible run passes through unphysical states on the way."""
    text = read(compressible.write_solution(str(tmp_path), steady=True))
    assert "pMinFactor" in text and "pMaxFactor" in text


# --- the comparison ---------------------------------------------------------

def test_matching_flow_is_reported_as_correct():
    out = flowtest._compare(0.100, 0.098, 0.11, True)
    assert "right for this engine" in out["verdict"]


def test_too_much_flow_says_make_it_smaller():
    out = flowtest._compare(0.130, 0.100, 0.15, True)
    assert out["cfd_vs_design_percent"] == pytest.approx(30.0)
    assert "smaller" in out["verdict"]


def test_too_little_flow_says_make_it_larger_and_warns_of_heat():
    out = flowtest._compare(0.070, 0.100, 0.15, True)
    assert "larger" in out["verdict"]
    assert "temperature" in out["verdict"]


def test_unchoked_nozzle_is_flagged():
    out = flowtest._compare(0.100, 0.098, 0.11, False)
    assert "not choked" in out.get("note", "")
