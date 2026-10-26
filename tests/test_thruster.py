"""The air-fed thruster: closed-form gas dynamics, checked against theory.

No solver, no external application -- these run everywhere and fast.
"""
from __future__ import annotations

import math

import pytest

from core.thruster import design as T


def test_cfm_round_trips_to_mass_flow():
    assert T.kg_s_to_cfm(T.cfm_to_kg_s(5.0)) == pytest.approx(5.0)


def test_free_air_conversion_is_the_standard_density():
    # 1 CFM of free air at sea level is about 0.02 kg/min.
    assert T.cfm_to_kg_s(1.0) * 60 == pytest.approx(0.0347, rel=0.02)


def test_choking_needs_about_0_9_bar_gauge():
    ratio = T.choked_pressure_ratio()
    assert ratio == pytest.approx(1.8929, abs=1e-3)


def test_supply_must_be_given():
    with pytest.raises(ValueError, match="give the air supply"):
        T.design(6.0)


def test_throat_follows_from_the_supply():
    """Double the air through the same conditions needs twice the throat area."""
    small = T.design(6.0, supply_cfm=5)
    large = T.design(6.0, supply_cfm=10)
    area_small = math.pi * (small.throat_diameter_mm / 2) ** 2
    area_large = math.pi * (large.throat_diameter_mm / 2) ** 2
    assert area_large == pytest.approx(2 * area_small, rel=1e-6)


def test_thrust_is_proportional_to_supply():
    """Nothing about the nozzle changes what the compressor can deliver."""
    a = T.design(6.0, supply_cfm=5).thrust_n
    b = T.design(6.0, supply_cfm=10).thrust_n
    assert b == pytest.approx(2 * a, rel=1e-6)


def test_exit_velocity_of_a_choked_convergent_nozzle_is_sonic():
    t = T.design(6.0, supply_cfm=5)
    throat_t = T.T_ATM * 2 / (T.GAMMA + 1)
    assert t.exit_velocity == pytest.approx(
        math.sqrt(T.GAMMA * T.R_AIR * throat_t), rel=1e-9)


def test_heating_raises_exit_velocity_as_root_temperature():
    cold = T.design(6.0, supply_cfm=5, chamber_temperature=300.0)
    hot = T.design(6.0, supply_cfm=5, chamber_temperature=1200.0)
    assert hot.exit_velocity == pytest.approx(2 * cold.exit_velocity, rel=1e-6)


def test_heating_needs_a_bigger_throat_for_the_same_flow():
    """Hot air is less dense, so the same mass needs more area."""
    cold = T.design(6.0, supply_cfm=5, chamber_temperature=300.0)
    hot = T.design(6.0, supply_cfm=5, chamber_temperature=1200.0)
    assert hot.throat_diameter_mm > cold.throat_diameter_mm


def test_low_supply_is_flagged_as_unchoked():
    t = T.design(0.4, supply_cfm=5)
    assert not t.choked
    assert any("chokes" in w or "choke" in w for w in t.warnings)


def test_high_supply_chokes():
    assert T.design(6.0, supply_cfm=5).choked


def test_divergent_nozzle_expands_beyond_the_throat():
    t = T.design(6.0, supply_cfm=5, divergent=True)
    assert t.exit_diameter_mm > t.throat_diameter_mm
    assert t.exit_velocity > T.design(6.0, supply_cfm=5).exit_velocity


def test_tiny_throat_is_flagged():
    t = T.design(6.0, supply_cfm=0.5)
    assert any("tolerance" in w for w in t.warnings)


def test_hot_design_warns_about_materials():
    t = T.design(6.0, supply_cfm=5, chamber_temperature=1000.0)
    assert any("aluminium" in w for w in t.warnings)


# --- endurance and the flight question --------------------------------------

def test_tank_endurance_scales_with_stored_air():
    flow = T.cfm_to_kg_s(5)
    small = T.endurance_from_tank(0.5, 200, flow)
    large = T.endurance_from_tank(1.0, 200, flow)
    # Tolerance covers the reported values being rounded for display.
    assert large["run_time_s"] == pytest.approx(2 * small["run_time_s"], rel=1e-3)


def test_stored_air_matches_the_gas_law():
    r = T.endurance_from_tank(1.0, 300, T.cfm_to_kg_s(5), usable_fraction=1.0)
    expected = (300e5 + T.P_ATM) * 0.001 / (T.R_AIR * T.T_ATM)
    assert r["stored_air_kg"] == pytest.approx(expected, rel=1e-4)


def test_an_air_fed_thruster_cannot_lift_its_own_tank():
    """The honest limit: newtons of thrust, kilograms of air supply."""
    t = T.design(6.0, supply_cfm=5)
    r = T.can_it_fly(t.thrust_n, 1.1, 300, t.supply_mass_flow)
    assert r["thrust_to_weight"] < 1.0
    assert "will not lift itself" in r["verdict"]


def test_zero_flow_is_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        T.endurance_from_tank(1.0, 300, 0.0)


# --- parts ------------------------------------------------------------------

def test_cold_build_is_three_parts():
    parts = T.parts_list(T.design(6.0, supply_cfm=5))
    assert len(parts) == 3, "a cold-gas thruster should stay very simple"


def test_adding_a_burner_adds_exactly_one_part_and_says_so():
    parts = T.parts_list(T.design(6.0, supply_cfm=5, chamber_temperature=1000))
    assert len(parts) == 4
    assert any("igniter" in p["part"] for p in parts)
    assert any("steel" in p["material"] for p in parts)
