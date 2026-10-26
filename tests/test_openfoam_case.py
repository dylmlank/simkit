"""Case generation, including the fork-dialect rules.

These write files and read them back; nothing here starts a solver, so the
suite stays fast. The dialect tests are the important ones: this machine has
both OpenFOAM forks installed, and a case written in the wrong dialect fails
minutes later with an error that never names the real cause.
"""
from __future__ import annotations

import os

import pytest

from core.openfoam import case
from core.openfoam.dialect import ESI, FOUNDATION, Dialect, detect

FOUND = Dialect(FOUNDATION, "13")
ESI_D = Dialect(ESI, "v2412")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- dialect rules ----------------------------------------------------------

def test_fork_is_detected_by_foamrun_not_version_string():
    assert detect(has_foam_run=True, version="13").flavor == FOUNDATION
    assert detect(has_foam_run=False, version="v2412").flavor == ESI


def test_the_two_forks_disagree_about_every_name():
    assert FOUND.viscosity_dict == "physicalProperties"
    assert ESI_D.viscosity_dict == "transportProperties"
    assert FOUND.turbulence_dict == "momentumTransport"
    assert ESI_D.turbulence_dict == "turbulenceProperties"
    assert FOUND.turbulence_model_key == "model"
    assert ESI_D.turbulence_model_key == "RASModel"
    assert FOUND.control_solver_key == "solver"
    assert ESI_D.control_solver_key == "application"
    assert FOUND.inside_point_key == "insidePoint"
    assert ESI_D.inside_point_key == "locationInMesh"


def test_run_command_matches_the_fork():
    # Foundation unified its solvers behind foamRun in v11.
    assert FOUND.run_command(steady=True) == "foamRun"
    assert FOUND.run_command(steady=False) == "foamRun"
    # ESI still ships one executable per algorithm.
    assert ESI_D.run_command(steady=True) == "simpleFoam"
    assert ESI_D.run_command(steady=False) == "pimpleFoam"


@pytest.mark.parametrize("dia,dict_name,model_key", [
    (FOUND, "momentumTransport", "model"),
    (ESI_D, "turbulenceProperties", "RASModel"),
])
def test_turbulence_dict_written_in_the_right_dialect(tmp_path, dia, dict_name,
                                                      model_key):
    path = case.write_momentum_transport(str(tmp_path), "kOmegaSST", dia)
    assert os.path.basename(path) == dict_name
    text = read(path)
    assert f"{model_key}           kOmegaSST" in text


@pytest.mark.parametrize("dia,expected", [(FOUND, "viscosityModel  constant"),
                                          (ESI_D, "transportModel  Newtonian")])
def test_viscosity_dict_written_in_the_right_dialect(tmp_path, dia, expected):
    path = case.write_physical(str(tmp_path), 1.5e-5, dia)
    assert expected in read(path)
    assert "1.5e-05" in read(path)


def test_snappy_seed_point_uses_the_right_keyword(tmp_path):
    esi = read(case.write_snappy(str(tmp_path), "b.stl", "body", [0, 0, 0],
                                 dia=ESI_D))
    foundation = read(case.write_snappy(str(tmp_path), "b.stl", "body", [0, 0, 0],
                                        dia=FOUND))
    assert "locationInMesh (0 0 0)" in esi
    assert "insidePoint (0 0 0)" in foundation


def test_stl_is_scaled_from_millimetres_to_metres(tmp_path):
    text = read(case.write_snappy(str(tmp_path), "part.stl", "body", [1, 0, 0],
                                  dia=ESI_D))
    # CAD exports mm; OpenFOAM works in m. Without this a 50 mm part becomes
    # a 50 m one and the drag is silently meaningless.
    assert "scale 0.001;" in text


# --- physics ----------------------------------------------------------------

def test_inlet_turbulence_follows_from_speed_and_length():
    values = case.inlet_turbulence(speed=10.0, length=0.5, intensity=0.05)
    # k = 1.5 (U I)^2
    assert values["k"] == pytest.approx(1.5 * (10.0 * 0.05) ** 2)
    # epsilon = Cmu^0.75 k^1.5 / l, with l = 0.07 L
    expected_eps = (0.09 ** 0.75) * values["k"] ** 1.5 / (0.07 * 0.5)
    assert values["epsilon"] == pytest.approx(expected_eps)
    assert values["omega"] == pytest.approx(
        values["k"] ** 0.5 / (0.09 ** 0.25 * 0.07 * 0.5))


def test_still_air_does_not_divide_by_zero():
    values = case.inlet_turbulence(speed=0.0, length=1.0)
    assert values["k"] > 0 and values["epsilon"] > 0


# --- boundary conditions ----------------------------------------------------

def test_patch_role_drives_every_field():
    patches = case.box_patches(two_d=True)
    roles = {p.name: p.role for p in patches}
    assert roles["inlet"] == "inlet"
    assert roles["frontAndBack"] == "empty"


def test_outlet_velocity_is_inletoutlet_not_zerogradient(tmp_path):
    """Backflow through a zeroGradient outlet is a classic way to blow up a run."""
    patches = case.box_patches(two_d=True)
    values = dict(case.inlet_turbulence(10, 1), velocity=[10, 0, 0])
    text = read(case.write_field(str(tmp_path), "U", patches, values))
    outlet = text.split("outlet")[1].split("}")[0]
    assert "inletOutlet" in outlet


def test_walls_get_wall_functions_for_turbulence_fields(tmp_path):
    patches = case.box_patches(two_d=True, ground=True)
    values = dict(case.inlet_turbulence(10, 1), velocity=[10, 0, 0])
    text = read(case.write_field(str(tmp_path), "epsilon", patches, values))
    assert "epsilonWallFunction" in text


def test_empty_patch_is_empty_in_every_field(tmp_path):
    patches = case.box_patches(two_d=True)
    values = dict(case.inlet_turbulence(10, 1), velocity=[10, 0, 0])
    for field in ("U", "p", "k", "epsilon", "nut"):
        text = read(case.write_field(str(tmp_path), field, patches, values))
        block = text.split("frontAndBack")[1]
        assert "type            empty;" in block, field


# --- mesh -------------------------------------------------------------------

def test_block_faces_are_wound_outward():
    """A reversed face makes blockMesh reject the whole mesh."""
    assert case.BOX_FACES["x-min"] == (0, 4, 7, 3)
    assert case.BOX_FACES["x-max"] == (2, 6, 5, 1)
    assert len({tuple(sorted(f)) for f in case.BOX_FACES.values()}) == 6


def test_inverted_domain_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="x-max"):
        case.write_block_mesh(str(tmp_path),
                              {"min": [1, 0, 0], "max": [0, 1, 1]},
                              (10, 10, 10), case.box_patches(False), ESI_D)


# --- control ----------------------------------------------------------------

def test_steady_control_uses_iterations_not_seconds(tmp_path):
    text = read(case.write_control(str(tmp_path), 500, 100, steady=True, dia=ESI_D))
    assert "deltaT          1;" in text
    assert "writeControl    timeStep;" in text
    assert "adjustTimeStep" not in text
    assert "application          simpleFoam;" in text


def test_transient_control_respects_the_courant_limit(tmp_path):
    text = read(case.write_control(str(tmp_path), 0.5, 0.1, 1e-4, steady=False,
                                   dia=FOUND))
    assert "adjustTimeStep  yes;" in text
    assert "maxCo" in text
    assert "solver          incompressibleFluid;" in text


def test_steady_solution_declares_residual_controls(tmp_path):
    text = read(case.write_solution(str(tmp_path), steady=True))
    assert "SIMPLE" in text and "residualControl" in text
    assert "relaxationFactors" in text


def test_walldist_is_always_present(tmp_path):
    """kOmegaSST and SpalartAllmaras refuse to start without it."""
    assert "wallDist" in read(case.write_schemes(str(tmp_path)))


def test_unknown_turbulence_model_is_rejected():
    with pytest.raises(ValueError, match="unknown turbulence model"):
        case.create("x", [1, 0, 0], {"min": [0, 0, 0], "max": [1, 1, 1]},
                    turbulence="magic")
