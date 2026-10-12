"""The one gRPC message bound: its config field, helper and container env.

``compute.grpc_max_message_mb`` is the single setting for the size of every
gRPC message between the engine, the runner and the grader. These tests pin
its schema bounds, the byte options the helper derives from it, and the
startup contract of the containers that read it from
``TOLOKAFORGE_GRPC_MAX_MESSAGE_MB``: unset means the default, anything set
but invalid fails startup instead of serving with a bound the peer does not
share.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import ComputeConfig, EvaluationConfig, ModelConfig, RunConfig
from tolokaforge.runner import __main__ as runner_main
from tolokaforge.runner.grpc_limits import (
    DEFAULT_MAX_MESSAGE_MB,
    MAX_MESSAGE_ENV_VAR,
    MAX_MESSAGE_MB_CEILING,
    MAX_MESSAGE_MB_FLOOR,
    channel_options,
    max_message_mb_from_env,
    resource_exhausted_message,
    validate_max_message_mb,
)

pytestmark = pytest.mark.unit

_MIB = 1024 * 1024


def _run_config(**extra: object) -> RunConfig:
    return RunConfig.model_validate(
        {
            "models": {"agent": ModelConfig(provider="openai", name="gpt-4")},
            "orchestrator": {},
            "evaluation": EvaluationConfig(output_dir="/tmp/grpc-limits"),
            **extra,
        }
    )


class TestComputeConfigField:
    def test_default_is_fifty_mib(self) -> None:
        assert ComputeConfig().grpc_max_message_mb == 50 == DEFAULT_MAX_MESSAGE_MB

    def test_run_config_without_compute_block_resolves_to_the_default(self) -> None:
        config = _run_config()

        assert config.compute is None
        assert (config.compute or ComputeConfig()).grpc_max_message_mb == DEFAULT_MAX_MESSAGE_MB

    def test_declared_value_is_kept(self) -> None:
        config = _run_config(compute={"grpc_max_message_mb": 512})

        assert config.compute is not None
        assert config.compute.grpc_max_message_mb == 512

    @pytest.mark.parametrize("value", [MAX_MESSAGE_MB_FLOOR, MAX_MESSAGE_MB_CEILING])
    def test_bounds_are_inclusive(self, value: int) -> None:
        assert ComputeConfig(grpc_max_message_mb=value).grpc_max_message_mb == value

    @pytest.mark.parametrize("value", [0, 3, 2048, -1])
    def test_out_of_range_values_are_refused(self, value: int) -> None:
        with pytest.raises(ValidationError) as exc:
            ComputeConfig(grpc_max_message_mb=value)

        assert "grpc_max_message_mb" in str(exc.value)

    def test_fractional_mib_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(grpc_max_message_mb=50.5)


class TestChannelOptions:
    def test_send_and_receive_are_both_bounded_in_bytes(self) -> None:
        assert dict(channel_options(50)) == {
            "grpc.max_send_message_length": 50 * _MIB,
            "grpc.max_receive_message_length": 50 * _MIB,
        }

    def test_ceiling_fits_grpc_signed_32_bit_option(self) -> None:
        for _, limit_bytes in channel_options(MAX_MESSAGE_MB_CEILING):
            assert limit_bytes <= 2**31 - 1

    @pytest.mark.parametrize("value", [3, 2048, True])
    def test_invalid_bound_is_refused(self, value: int) -> None:
        with pytest.raises(ValueError):
            channel_options(value)

    def test_validator_matches_config_bounds(self) -> None:
        assert validate_max_message_mb(MAX_MESSAGE_MB_FLOOR) == MAX_MESSAGE_MB_FLOOR
        assert validate_max_message_mb(MAX_MESSAGE_MB_CEILING) == MAX_MESSAGE_MB_CEILING
        with pytest.raises(ValueError):
            validate_max_message_mb(MAX_MESSAGE_MB_FLOOR - 1)
        with pytest.raises(ValueError):
            validate_max_message_mb(MAX_MESSAGE_MB_CEILING + 1)


class TestMaxMessageMbFromEnv:
    def test_unset_means_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MAX_MESSAGE_ENV_VAR, raising=False)

        assert max_message_mb_from_env() == DEFAULT_MAX_MESSAGE_MB

    def test_empty_means_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, "  ")

        assert max_message_mb_from_env() == DEFAULT_MAX_MESSAGE_MB

    def test_valid_value_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, "256")

        assert max_message_mb_from_env() == 256

    @pytest.mark.parametrize("raw", ["fifty", "50.5", "0x40", "3", "2048", "-50"])
    def test_invalid_value_fails_naming_variable_and_range(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, raw)

        with pytest.raises(ValueError) as exc:
            max_message_mb_from_env()

        message = str(exc.value)
        assert MAX_MESSAGE_ENV_VAR in message
        assert repr(raw) in message
        assert f"between {MAX_MESSAGE_MB_FLOOR} and {MAX_MESSAGE_MB_CEILING}" in message


class TestRunnerServerReadsTheBound:
    """The runner container serves with the bound its env names."""

    def test_get_config_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, "128")

        assert runner_main.get_config()["grpc_max_message_mb"] == 128

    def test_get_config_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MAX_MESSAGE_ENV_VAR, raising=False)

        assert runner_main.get_config()["grpc_max_message_mb"] == DEFAULT_MAX_MESSAGE_MB

    def test_invalid_env_fails_startup_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, "huge")

        with pytest.raises(ValueError, match=MAX_MESSAGE_ENV_VAR):
            runner_main.get_config()


def test_resource_exhausted_message_names_limit_and_setting() -> None:
    message = resource_exhausted_message("GetState", 64, "Received message larger than max")

    assert "GetState" in message
    assert "64 MiB" in message
    assert "compute.grpc_max_message_mb" in message
    assert "Received message larger than max" in message
