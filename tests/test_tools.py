"""The tool layer: schemas, dispatch, and how failures are reported.

Fast and offline. What matters here is that the two surfaces (MCP server and
agent) cannot drift apart, and that a tool which cannot do its job returns a
sentence a caller can act on instead of a stack trace.
"""
from __future__ import annotations

import pytest

from core import tools


def test_every_tool_has_a_handler_and_vice_versa():
    assert {t["name"] for t in tools.TOOLS} == set(tools.HANDLERS)


def test_all_four_applications_are_covered():
    prefixes = {name.split("_")[0] for name in tools.HANDLERS}
    assert {"freecad", "fusion", "openrocket", "openfoam"} <= prefixes


@pytest.mark.parametrize("spec", tools.TOOLS, ids=lambda s: s["name"])
def test_schema_is_well_formed(spec):
    assert spec["description"].strip(), f"{spec['name']} has no description"
    schema = spec["input_schema"]
    assert schema["type"] == "object"
    for name in schema["required"]:
        assert name in schema["properties"], (
            f"{spec['name']} requires {name!r} but does not declare it")
    for name, prop in schema["properties"].items():
        assert "type" in prop, f"{spec['name']}.{name} has no type"


@pytest.mark.parametrize("spec", tools.TOOLS, ids=lambda s: s["name"])
def test_handler_accepts_its_declared_arguments(spec):
    """A schema that names an argument the handler cannot take is a live bug."""
    import inspect

    signature = inspect.signature(tools.HANDLERS[spec["name"]])
    accepted = set(signature.parameters)
    declared = set(spec["input_schema"]["properties"])
    assert declared <= accepted, (
        f"{spec['name']} declares {declared - accepted} that the handler "
        "will not accept")


def test_unknown_tool_lists_the_alternatives():
    result = tools.dispatch("not_a_tool", {})
    assert "Unknown tool" in result
    assert "survey" in result


def test_bad_arguments_are_explained_not_raised():
    result = tools.dispatch("freecad_new_document", {"wrong": 1})
    assert "bad arguments" in result


def test_failures_come_back_as_text():
    """Dispatch must never raise: the caller has to be able to read and retry."""
    result = tools.dispatch("openfoam_status", {"name": "no_such_case_at_all"})
    assert isinstance(result, str)
    assert "failed" in result.lower()


def test_none_arguments_are_dropped_so_defaults_apply():
    # A model that fills every optional field with null should still get the
    # handler's own defaults rather than an explicit None.
    result = tools.dispatch("openrocket_examples", {"name": None, "save_as": None})
    assert "example designs" in result


def test_survey_reports_every_application():
    text = tools.survey()
    for name in ("Fusion 360", "FreeCAD", "OpenRocket", "OpenFOAM"):
        assert name in text
