"""A run config's ``capabilities:`` override keys, and the refusal of one no override
recognises.

:func:`_apply_config_overrides` raises :class:`ValueError` on an unknown key, with a
message that (a) names every offending key, (b) names the recognised keys, and (c)
points the reader at the contract doc. ``config validate`` reports that key, or a
recognised override the config's capabilities cannot build with, on every model
config, fallbacks included, as an ERROR at ``<path>.capabilities``, and ``run`` /
``prepare`` / ``worker`` refuse to start naming every one of them.

``test_recognised_keys_are_the_documented_set`` keeps the set and the body from
drifting apart: every key in :data:`_RECOGNISED_OVERRIDE_KEYS` must appear as a
literal inside the function body, so a key cannot join the allowlist without the
function learning how to translate it.
"""

from __future__ import annotations

import inspect
import pickle
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.core.config_validator import Severity, validate_run_config
from tolokaforge.core.llm.presets import (
    _RECOGNISED_OVERRIDE_KEYS,
    CapabilityOverrideError,
    _apply_config_overrides,
)
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


class TestApplyConfigOverridesRejectsUnknown:
    @pytest.mark.parametrize("value", [0, -1, True, None, "600", float("inf"), float("nan")])
    def test_timeout_override_refuses_invalid_values(self, value) -> None:
        with pytest.raises(ValueError, match="finite positive"):
            _apply_config_overrides({}, {"api_call_timeout_s": value})

    def test_timeout_override_is_per_model(self) -> None:
        from tolokaforge.core.llm import build_capabilities

        ordinary = build_capabilities("openai/gpt-6-sol", "openrouter")
        judge = build_capabilities("openai/gpt-6-sol", "openrouter", {"api_call_timeout_s": 600})
        assert judge.api_call_timeout_s == 600
        assert ordinary.api_call_timeout_s != 600

    def test_unknown_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown capability override keys"):
            _apply_config_overrides({}, {"some_typo": True})

    def test_error_lists_offending_keys(self) -> None:
        # Sorted, so "alpha" comes before "bravo" regardless of dict order.
        with pytest.raises(ValueError, match=r"\['alpha', 'bravo'\]"):
            _apply_config_overrides({}, {"bravo": 1, "alpha": 2})

    def test_error_points_at_contract_doc(self) -> None:
        with pytest.raises(ValueError, match=r"docs/CONFIG\.md § Model Capability Presets"):
            _apply_config_overrides({}, {"nope": 1})

    def test_recognised_keys_do_not_raise(self) -> None:
        # ``supports_seed`` is in the set; ensure no ValueError.
        _apply_config_overrides({}, {"supports_seed": True})

    def test_empty_overrides_do_not_raise(self) -> None:
        _apply_config_overrides({}, {})

    def test_mixed_known_and_unknown_still_raises(self) -> None:
        # Having one recognised key does NOT suppress the failure on the
        # offending one — the unknown key must still surface.
        with pytest.raises(ValueError, match=r"\['rogue_key'\]"):
            _apply_config_overrides({}, {"supports_seed": True, "rogue_key": "oops"})

    def test_recognised_keys_are_the_documented_set(self) -> None:
        """Every key in the allowlist must be referenced in the body.

        Pins the invariant that the set and the ``if "<key>" in overrides``
        branches cannot drift apart. A future contributor adding a key to
        the set without a translation branch hits this guard.
        """
        src = inspect.getsource(_apply_config_overrides)
        for key in _RECOGNISED_OVERRIDE_KEYS:
            assert f'"{key}"' in src or f"'{key}'" in src, (
                f"{key!r} is in _RECOGNISED_OVERRIDE_KEYS but not referenced "
                f"in _apply_config_overrides body"
            )


_MODEL = {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"}
_TYPO = {**_MODEL, "capabilities": {"not_a_key": 1}}
_UNBUILDABLE = {
    "provider": "openrouter",
    "name": "openai/gpt-5.2",
    "capabilities": {"gemini_drop_placeholder_signature": True},
}
_TYPO_REASON = "['not_a_key']"
_UNBUILDABLE_REASON = "OpenAIReasoningCodec() takes no arguments"


def _run_config(models: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluation": {
            "tasks_glob": "tasks/**/task.yaml",
            "output_dir": "output",
            "harness_adapter": {"type": "frozen_mcp_core"},
        },
        "orchestrator": {"workers": 1, "repeats": 1},
        "models": {"user": _MODEL, **models},
    }


class TestEveryConfigsOverridesAreChecked:
    @pytest.mark.parametrize(
        ("models", "refused", "reason"),
        [
            pytest.param({"agent": _TYPO}, ["models.agent"], _TYPO_REASON, id="agent"),
            pytest.param(
                {"agent": {**_MODEL, "fallbacks": [_TYPO]}},
                ["models.agent.fallbacks[0]"],
                _TYPO_REASON,
                id="fallback",
            ),
            pytest.param(
                {"agent": _MODEL, "judge": _TYPO},
                ["models.judge"],
                _TYPO_REASON,
                id="judge-never-built",
            ),
            pytest.param(
                {"agent": _TYPO, "judge": _TYPO},
                ["models.agent", "models.judge"],
                _TYPO_REASON,
                id="two",
            ),
            pytest.param(
                {"agent": _UNBUILDABLE},
                ["models.agent"],
                _UNBUILDABLE_REASON,
                id="unbuildable",
            ),
            pytest.param(
                {"agent": {**_UNBUILDABLE, "temperature": 0.7}},
                ["models.agent"],
                _UNBUILDABLE_REASON,
                id="unbuildable-with-temperature",
            ),
            pytest.param(
                {"agent": _MODEL, "judge": _UNBUILDABLE},
                ["models.judge"],
                _UNBUILDABLE_REASON,
                id="unbuildable-judge-never-built",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(["run", "--dry-run"], id="run"),
            pytest.param(["prepare", "--run-dir", "{run_dir}"], id="prepare"),
            pytest.param(["worker", "--run-dir", "{run_dir}"], id="worker"),
        ],
    )
    def test_validate_and_run_start_refuse_the_same_configs(
        self,
        tmp_path: Path,
        models: dict[str, Any],
        refused: list[str],
        reason: str,
        command: list[str],
    ) -> None:
        config = tmp_path / "run.yaml"
        config.write_text(yaml.safe_dump(_run_config(models)))
        paths = [f"{path}.capabilities" for path in refused]

        errors = [
            (i.path, i.message)
            for i in validate_run_config(_run_config(models)).issues
            if i.severity is Severity.ERROR
        ]
        assert [path for path, _ in errors] == paths
        assert all(reason in message for _, message in errors)

        argv = [arg.format(run_dir=tmp_path / "run_dir") for arg in command]
        started = CliRunner().invoke(cli, [argv[0], "--config", str(config), *argv[1:]])
        assert started.exit_code == 1, started.output
        refusals = [line for line in started.output.splitlines() if ".capabilities: " in line]
        assert [line.removeprefix("Error: ").split(": ", 1)[0] for line in refusals] == paths
        assert all(reason in line for line in refusals)

    def test_the_refusal_survives_a_pickle_round_trip(self) -> None:
        err = CapabilityOverrideError(path="models.agent.capabilities", reason="unknown keys")
        back = pickle.loads(pickle.dumps(err))
        assert (type(back), str(back), back.path, back.reason) == (
            CapabilityOverrideError,
            str(err),
            "models.agent.capabilities",
            "unknown keys",
        )
