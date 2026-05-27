"""Tests for DockerRunnerAdapter.cleanup_trial pass-through (issue #132).

The adapter binds a ``trial_id`` and forwards cleanup to the underlying
RunnerClient — calling it should match the per-trial interface (no extra args)
and target this adapter's trial_id only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.docker_adapter import DockerRunnerAdapter

pytestmark = pytest.mark.unit


def test_cleanup_trial_passes_through_to_runner_client_with_bound_trial_id():
    runner = MagicMock()
    runner.cleanup_trial.return_value = {"success": True, "error": None}
    adapter = DockerRunnerAdapter(runner_client=runner, trial_id="TASK-001:3")

    result = adapter.cleanup_trial()

    runner.cleanup_trial.assert_called_once_with(trial_id="TASK-001:3")
    assert result == {"success": True, "error": None}


def test_cleanup_trial_returns_underlying_error_unchanged():
    runner = MagicMock()
    runner.cleanup_trial.return_value = {"success": False, "error": "trial not found"}
    adapter = DockerRunnerAdapter(runner_client=runner, trial_id="x:0")

    result = adapter.cleanup_trial()

    assert result == {"success": False, "error": "trial not found"}
