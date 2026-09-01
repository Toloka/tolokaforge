"""Both branches of the emit-payload schema check on the reusable suite fire.

The two shipping adapters (Native, terminal-bench) both inherit the default
empty payload, so the non-empty branch of the suite's schema check would be
dead code without a subject that exercises it. A ``pytester`` in-process
session drops a synthetic subclass in a tmpdir and runs the suite against
two fake adapters — one returning ``{}`` (empty-payload short-circuit skips
the check) and one returning the ``test_execution`` payload
:meth:`~tolokaforge_coding_harnesses.adapter_support.CodingHarnessAdapterMixin.emit_test_execution_grading`
emits (schema check runs and passes).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.canonical


_SUITE_SUBCLASS_SOURCE = '''
"""Synthetic subclasses exercising both branches of the emit-payload check."""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters._task_loader import GradingSource, GradingSourceKind
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
)
from tolokaforge.core.models import TaskConfig, InitialStateConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite


class _FakeAdapterBase:
    """Structural stand-in for a real adapter — resolves every grading-contract slot."""

    requires_docker_cli_in_runner = False
    grades_from_task_grading_file = False
    syncs_adapter_env_to_state = False

    def grading_source(self, task, task_dir):
        return GradingSource(
            kind=GradingSourceKind.UNINTERROGABLE,
            path=None,
            reason="synthetic adapter for suite self-check",
        )

    def grading_tool_inventory(self, task, task_dir):
        return ToolInventory.unresolvable()

    def grading_replay_world(self, task, task_dir):
        return ReplayWorld.unresolvable()

    def grading_seeded_tables(self, task, task_dir):
        return SeededTablesLayer.unresolvable()

    def preferred_grader_kind(self):
        return "composite"


class _FakeAdapterEmptyPayload(_FakeAdapterBase):
    def emit_runner_grading_payload(self, task_id):
        return {}


class _FakeAdapterNonEmptyPayload(_FakeAdapterBase):
    def emit_runner_grading_payload(self, task_id):
        return {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "grading_method": "test_execution",
        }


def _a_synthetic_task():
    task = TaskConfig(
        task_id="synthetic_task",
        description="synthetic task for suite self-check",
        initial_state=InitialStateConfig(),
    )
    return task, Path("/nonexistent-task-dir-suite-self-check")


class TestEmptyPayloadShortCircuits(AdapterGradingContractSuite):
    @pytest.fixture
    def adapter(self):
        return _FakeAdapterEmptyPayload()

    @pytest.fixture
    def task_and_dir(self):
        return _a_synthetic_task()


class TestNonEmptyPayloadValidates(AdapterGradingContractSuite):
    @pytest.fixture
    def adapter(self):
        return _FakeAdapterNonEmptyPayload()

    @pytest.fixture
    def task_and_dir(self):
        return _a_synthetic_task()
'''


def test_both_branches_of_the_emit_payload_schema_check_fire(
    pytester: pytest.Pytester,
) -> None:
    """Drop the synthetic subclasses in a tmpdir, run pytest against them, and
    read the outcome: empty-payload branch skips the schema check, non-empty
    branch runs it and passes."""
    pytester.makepyfile(test_suite_self_check=_SUITE_SUBCLASS_SOURCE)

    result = pytester.runpytest("-v", "--no-header", "-p", "no:cacheprovider")

    assert result.ret == 0, (
        "the synthetic subclasses failed under the reusable suite: "
        f"exit={result.ret}, stdout tail={result.stdout.lines[-40:]!r}"
    )

    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) >= 21, (
        f"expected the two subclasses' 11 test methods each to run (~22 total), "
        f"got outcomes={outcomes!r}"
    )
    assert outcomes.get("skipped", 0) == 1, (
        f"expected exactly one skip — the empty-payload branch of the schema "
        f"check on TestEmptyPayloadShortCircuits — got outcomes={outcomes!r}"
    )

    result.stdout.fnmatch_lines(
        [
            "*TestEmptyPayloadShortCircuits*"
            "test_emit_runner_grading_payload_constructs_a_valid_runner_grading_config*SKIPPED*",
        ]
    )
    result.stdout.fnmatch_lines(
        [
            "*TestNonEmptyPayloadValidates*"
            "test_emit_runner_grading_payload_constructs_a_valid_runner_grading_config*PASSED*",
        ]
    )
