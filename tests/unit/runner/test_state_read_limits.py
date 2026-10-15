"""The one bound on reading a trial's state back from its state holder.

``compute.state_read_timeout_s`` is how long the runner waits for one read of the
live state an MCP server subprocess or an app world service holds. These tests pin
its schema, the runner container's startup contract for
``TOLOKAFORGE_STATE_READ_TIMEOUT_S`` (unset means the default, anything set but
invalid fails startup), and the ordering every derived runner deadline keeps: each
outlasts the holder read it waits on, so raising the bound lets a slow read through
and a hung holder still fails with the read's own cause.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import ComputeConfig, EvaluationConfig, ModelConfig, RunConfig
from tolokaforge.runner import __main__ as runner_main
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.state_read_limits import (
    DEFAULT_STATE_READ_TIMEOUT_S,
    STATE_READ_SETTING,
    STATE_READ_TIMEOUT_ENV_VAR,
    StateReadDeadlines,
    state_read_timeout_s_from_env,
    validate_state_read_timeout_s,
)

pytestmark = pytest.mark.unit


def _run_config(**extra: object) -> RunConfig:
    return RunConfig.model_validate(
        {
            "models": {"agent": ModelConfig(provider="openai", name="gpt-4")},
            "orchestrator": {},
            "evaluation": EvaluationConfig(output_dir="/tmp/state-read-limits"),
            **extra,
        }
    )


class TestComputeConfigField:
    def test_default_is_twenty_seconds(self) -> None:
        assert ComputeConfig().state_read_timeout_s == 20.0 == DEFAULT_STATE_READ_TIMEOUT_S

    def test_run_config_without_compute_block_resolves_to_the_default(self) -> None:
        config = _run_config()

        assert config.compute is None
        assert (config.compute or ComputeConfig()).state_read_timeout_s == 20.0

    @pytest.mark.parametrize("value", [180, 0.5, 3600.0])
    def test_declared_value_is_kept(self, value: float) -> None:
        config = _run_config(compute={"state_read_timeout_s": value})

        assert config.compute is not None
        assert config.compute.state_read_timeout_s == float(value)

    @pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True, "180"])
    def test_a_value_that_is_not_a_positive_finite_number_is_refused(self, value: object) -> None:
        with pytest.raises(ValidationError) as exc:
            ComputeConfig(state_read_timeout_s=value)

        assert "state_read_timeout_s" in str(exc.value)


class TestValidator:
    @pytest.mark.parametrize("value", [0.001, 20, 180.0])
    def test_positive_finite_seconds_are_accepted_as_float(self, value: float) -> None:
        accepted = validate_state_read_timeout_s(value)

        assert accepted == float(value)
        assert isinstance(accepted, float)

    @pytest.mark.parametrize("value", [0, -5.0, math.inf, math.nan, True, "20"])
    def test_anything_else_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError):
            validate_state_read_timeout_s(value)  # type: ignore[arg-type]


class TestStateReadTimeoutFromEnv:
    def test_unset_means_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(STATE_READ_TIMEOUT_ENV_VAR, raising=False)

        assert state_read_timeout_s_from_env() == DEFAULT_STATE_READ_TIMEOUT_S

    def test_empty_means_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STATE_READ_TIMEOUT_ENV_VAR, "  ")

        assert state_read_timeout_s_from_env() == DEFAULT_STATE_READ_TIMEOUT_S

    @pytest.mark.parametrize(("raw", "expected"), [("180", 180.0), ("180.0", 180.0), ("2.5", 2.5)])
    def test_valid_value_is_read(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
    ) -> None:
        monkeypatch.setenv(STATE_READ_TIMEOUT_ENV_VAR, raw)

        assert state_read_timeout_s_from_env() == expected

    @pytest.mark.parametrize("raw", ["slow", "0", "-20", "inf", "nan", "20s"])
    def test_invalid_value_fails_naming_the_variable(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(STATE_READ_TIMEOUT_ENV_VAR, raw)

        with pytest.raises(ValueError) as exc:
            state_read_timeout_s_from_env()

        assert STATE_READ_TIMEOUT_ENV_VAR in str(exc.value)
        assert repr(raw) in str(exc.value)


class TestDeadlines:
    def test_defaults_are_the_runners_long_standing_values(self) -> None:
        deadlines = StateReadDeadlines()

        assert deadlines.holder_read_s == 20.0
        assert deadlines.judge_db_read_s == 30.0
        assert deadlines.final_state_read_s == 300.0
        assert deadlines.grade_trial_s == 600.0

    @pytest.mark.parametrize("holder_read_s", [0.5, 20.0, 100.0, 180.0, 900.0, 3600.0])
    def test_every_caller_outlasts_the_holder_read_it_waits_on(self, holder_read_s: float) -> None:
        """A caller that gave up first would report its own timeout, not the holder's cause."""
        deadlines = StateReadDeadlines(holder_read_s=holder_read_s)

        assert deadlines.judge_db_read_s > deadlines.holder_read_s
        assert deadlines.final_state_read_s > deadlines.holder_read_s
        # A hash-graded GradeTrial reads the holder twice: before grading and
        # after the golden replay.
        assert deadlines.grade_trial_s > 2 * deadlines.holder_read_s

    def test_a_cold_world_read_fits_a_raised_bound_end_to_end(self) -> None:
        """A world that takes ~100 s to load on first contact is read once the bound allows it."""
        deadlines = StateReadDeadlines(holder_read_s=180.0)

        assert (
            min(
                deadlines.holder_read_s,
                deadlines.judge_db_read_s,
                deadlines.final_state_read_s,
                deadlines.grade_trial_s,
            )
            > 100.0
        )

    def test_an_invalid_bound_is_refused(self) -> None:
        with pytest.raises(ValueError):
            StateReadDeadlines(holder_read_s=0)

    def test_timeout_error_names_the_bound_the_setting_and_the_variable(self) -> None:
        error = StateReadDeadlines(holder_read_s=45.0).holder_timeout_error("MCP server")

        message = str(error)
        assert isinstance(error, TimeoutError)
        assert "the MCP server did not answer within 45s" in message
        assert f"`{STATE_READ_SETTING}`" in message
        assert STATE_READ_SETTING == "compute.state_read_timeout_s"
        assert STATE_READ_TIMEOUT_ENV_VAR in message


class TestRunnerReadsTheBound:
    """The runner container reads the bound its env names and derives its deadlines from it."""

    def test_get_config_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STATE_READ_TIMEOUT_ENV_VAR, "180")

        assert runner_main.get_config()["state_read_timeout_s"] == 180.0

    def test_get_config_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(STATE_READ_TIMEOUT_ENV_VAR, raising=False)

        assert runner_main.get_config()["state_read_timeout_s"] == DEFAULT_STATE_READ_TIMEOUT_S

    def test_invalid_env_fails_startup_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STATE_READ_TIMEOUT_ENV_VAR, "forever")

        with pytest.raises(ValueError, match=STATE_READ_TIMEOUT_ENV_VAR):
            runner_main.get_config()

    async def test_runner_server_hands_the_bound_to_its_service(self) -> None:
        server = runner_main.RunnerServer(
            db_service_url="http://127.0.0.1:9", port=0, state_read_timeout_s=180.0
        )
        await server.start()
        try:
            assert server.service is not None
            assert server.service.state_read_deadlines.holder_read_s == 180.0
        finally:
            await server.stop(grace_period=0)

    def test_service_derives_its_deadlines_from_the_bound(self) -> None:
        service = RunnerServiceImpl(MagicMock(), state_read_timeout_s=180.0)
        try:
            assert service.state_read_deadlines == StateReadDeadlines(holder_read_s=180.0)
        finally:
            service.shutdown()

    def test_service_defaults_to_the_default_bound(self) -> None:
        service = RunnerServiceImpl(MagicMock())
        try:
            assert service.state_read_deadlines.holder_read_s == DEFAULT_STATE_READ_TIMEOUT_S
        finally:
            service.shutdown()
