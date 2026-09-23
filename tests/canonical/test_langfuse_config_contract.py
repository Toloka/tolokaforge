"""The plugin-owned configuration is strict, round-trippable and engine-independent."""

import pytest
from pydantic import ValidationError
from tolokaforge_langfuse.config import LangfuseConfig

from tolokaforge.core.models import TracingConfig

pytestmark = pytest.mark.canonical


def test_langfuse_settings_wire_shape(canon_snapshot):
    config = LangfuseConfig()
    canon_snapshot("langfuse_config_contract").assert_match(
        {"wire": config.model_dump_json()}, "defaults.json"
    )
    assert LangfuseConfig.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LangfuseConfig.model_validate({"expect_projct": "pilot"})


@pytest.mark.parametrize(
    "field",
    [
        "expect_project",
        "project",
        "project_id",
        "environments",
        "attach",
        "gradings",
        "projection",
        "attach_api_base",
        "attach_timeout_s",
        "attach_budget_s",
        "profile",
        "environment",
        "model_name_normalizer",
        "model_name_rules",
    ],
)
def test_receiver_fields_are_rejected_outside_the_plugin_namespace(field):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TracingConfig.model_validate({field: "legacy"})
