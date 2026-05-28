"""Round-trip contract for ``ToolSchema.tool_config``.

The field is the carrier for per-tool ``__init__`` kwargs that the runner
splats into the tool class constructor (e.g. ``MobileTool(apps={...})``).
Pydantic must preserve it exactly across ``model_dump_json`` /
``model_validate_json`` so the runner sees what the adapter wrote.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.models import ToolSchema

pytestmark = pytest.mark.unit


def _schema_with_apps() -> ToolSchema:
    return ToolSchema(
        name="mobile",
        description="phone app interaction",
        parameters={"type": "object", "properties": {}},
        tool_config={
            "apps": {
                "CityMap": "http://mock-web:8080/task/mobile/app_citymap/",
                "Notepad": "http://mock-web:8080/task/mobile/app_notepad/",
            },
            "initial_app": "CityMap",
        },
    )


def test_tool_config_defaults_to_empty_dict():
    schema = ToolSchema(
        name="calculator",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    assert schema.tool_config == {}


def test_tool_config_round_trips_through_json():
    original = _schema_with_apps()
    rehydrated = ToolSchema.model_validate_json(original.model_dump_json())
    assert rehydrated.tool_config == original.tool_config
    assert rehydrated.tool_config["apps"]["CityMap"].startswith("http://mock-web")


def test_tool_config_is_independent_of_source():
    schema = _schema_with_apps()
    assert schema.source is None
    assert schema.tool_config != {}
