"""A route-prefixed model name that misses the preset its last segment matches.

``self-hosted/<model>`` resolves to ``default`` when no preset claims the
route prefix, even though ``<model>`` alone matches a preset. ``config
validate`` warns once per model config, fallbacks included, with both
remedies in its hint; the run logs one event per config carrying the same
fields and remedy. Driven through the real
``validate_run_config`` and the orchestrator's task load, with the overlay
installed the way the CLI installs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from tests.unit.test_orchestrator_strict_task_load import _make_task, _RaisingStubAdapter
from tolokaforge.core.config_validator import Severity, ValidationIssue, validate_run_config
from tolokaforge.core.llm.presets import (
    UNCLAIMED_ROUTE_FAMILY,
    UnclaimedRouteFamily,
    set_overlay_path,
    unclaimed_route_family,
)
from tolokaforge.core.models import EvaluationConfig, ModelConfig, OrchestratorConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

_CANARY_OVERLAY = {
    "presets": {
        "canary_family": {
            "match": ["tolokaforge-canary-family-*"],
            "prompt_policy": "dict_map_hints",
        }
    }
}

_CONTROLS = [
    "tolokaforge-canary-family-1",
    "self-hosted/tolokaforge-canary-unclaimed",
    "self-hosted/qwen3.6-35b-a3b",
]


@pytest.fixture
def canary_overlay(write_overlay: Callable[[dict], str]) -> None:
    set_overlay_path(write_overlay(_CANARY_OVERLAY))


def _run(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "models": {
            "agent": {"provider": "openai", **agent},
            "user": {"provider": "openrouter", "name": "openai/gpt-4o-mini"},
        },
        "orchestrator": {"workers": 1, "repeats": 1, "max_turns": 10},
        "evaluation": {"tasks_glob": "tasks/**/task.yaml", "output_dir": "out"},
    }


def _warnings(raw: dict[str, Any]) -> dict[str, ValidationIssue]:
    return {
        issue.path: issue
        for issue in validate_run_config(raw).issues
        if issue.severity is Severity.WARNING and issue.path.endswith(".name")
    }


def _load_tasks_logging(
    agent: ModelConfig, caplog: pytest.LogCaptureFixture
) -> list[logging.LogRecord]:
    config = RunConfig(
        models={"agent": agent, "user": ModelConfig(provider="openai", name="gpt-4o-mini")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/route_family_warning"),
    )
    orchestrator = Orchestrator(config)
    orchestrator.adapter = _RaisingStubAdapter(
        {}, tasks={"TASK-A": _make_task("TASK-A")}, raises=set()
    )
    with caplog.at_level(logging.WARNING):
        orchestrator.load_tasks()
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage() == UNCLAIMED_ROUTE_FAMILY
    ]


class TestConfigValidate:
    def test_a_bundled_family_missed_behind_a_route_prefix_is_warned(self) -> None:
        warned = _warnings(_run({"name": "self-hosted/nova-pro-v1"}))
        assert list(warned) == ["models.agent.name"]
        assert "matches preset 'aws_nova'" in warned["models.agent.name"].message

    def test_an_overlay_family_is_warned_for_the_model_and_its_fallback(
        self, canary_overlay: None
    ) -> None:
        raw = _run(
            {
                "name": "self-hosted/tolokaforge-canary-family-1",
                "fallbacks": [
                    {"provider": "openai", "name": "self-hosted/tolokaforge-canary-family-2"}
                ],
            }
        )
        warned = _warnings(raw)
        assert list(warned) == ["models.agent.name", "models.agent.fallbacks[0].name"]
        assert all("matches preset 'canary_family'" in issue.message for issue in warned.values())

    @pytest.mark.parametrize("name", _CONTROLS)
    def test_a_name_that_resolves_as_its_family_or_names_none_is_not_warned(
        self, name: str, canary_overlay: None
    ) -> None:
        assert _warnings(_run({"name": name})) == {}

    def test_the_warning_does_not_block_the_run(self) -> None:
        assert validate_run_config(_run({"name": "self-hosted/nova-pro-v1"})).ok


class TestMessage:
    def test_it_states_the_miss_once_and_leaves_the_path_to_the_issue(self) -> None:
        issue = _warnings(_run({"name": "self-hosted/novasky-t1"}))["models.agent.name"]
        assert issue.message == (
            "'self-hosted/novasky-t1' (provider 'openai') resolves to the 'default' preset, "
            "but its last segment 'novasky-t1' matches preset 'aws_nova'"
        )
        assert str(issue).count("models.agent.name") == 1

    def test_its_hint_offers_the_overlay_before_the_conditional_rename(self) -> None:
        hint = _warnings(_run({"name": "self-hosted/novasky-t1"}))["models.agent.name"].hint
        overlay = hint.index("add an overlay preset whose match covers the full name")
        rename = hint.index(
            "when the route also serves the unprefixed name, name the model 'novasky-t1'"
        )
        assert overlay < rename
        assert "match: ['*/novasky-t1'] or match: ['self-hosted/novasky-t1']" in hint

    def test_it_never_asserts_what_the_model_is(self) -> None:
        issue = _warnings(_run({"name": "self-hosted/novasky-t1"}))["models.agent.name"]
        assert "Nova" not in f"{issue.message} {issue.hint}".replace("aws_nova", "")


class TestRunStart:
    def test_the_run_logs_one_event_carrying_the_config_validate_remedy(
        self, canary_overlay: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = "self-hosted/tolokaforge-canary-family-1"
        issue = _warnings(_run({"name": name}))["models.agent.name"]
        [record] = _load_tasks_logging(ModelConfig(provider="openai", name=name), caplog)
        logged = {
            key: getattr(record, key)
            for key in ("path", "model_name", "provider", "last_segment", "family", "remedy")
        }
        assert logged == {
            "path": issue.path,
            "model_name": name,
            "provider": "openai",
            "last_segment": "tolokaforge-canary-family-1",
            "family": "canary_family",
            "remedy": issue.hint,
        }

    @pytest.mark.parametrize("name", _CONTROLS)
    def test_a_control_logs_nothing(
        self, name: str, canary_overlay: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert _load_tasks_logging(ModelConfig(provider="openai", name=name), caplog) == []


class TestUnclaimedRouteFamily:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "self-hosted/nova-pro-v1",
                UnclaimedRouteFamily(
                    "self-hosted/nova-pro-v1", "openai", "nova-pro-v1", "aws_nova"
                ),
            ),
            ("nova-pro-v1", None),
            ("self-hosted/tolokaforge-canary-unclaimed", None),
            ("self-hosted/qwen3.6-35b-a3b", None),
        ],
    )
    def test_it_reports_only_a_default_route_whose_last_segment_has_a_preset(
        self, name: str, expected: UnclaimedRouteFamily | None
    ) -> None:
        assert unclaimed_route_family(name, "openai") == expected
