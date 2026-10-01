"""A run config's ``capabilities:`` override keys, and the refusal of one no override
recognises.

:func:`_apply_config_overrides` raises :class:`ValueError` on an unknown key, with a
message that (a) names every offending key, (b) names the recognised keys, and (c)
points the reader at the contract doc. ``config validate`` reports the same key on
every model config, fallbacks included, as an ERROR at ``<path>.capabilities``, and
``run`` / ``prepare`` / ``worker`` refuse to start naming every one of them.

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
    def test_unknown_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown capability override keys"):
            _apply_config_overrides({}, {"some_typo": True})

    def test_error_lists_offending_keys(self) -> None:
        # Sorted, so "alpha" comes before "bravo" regardless of dict order.
        with pytest.raises(ValueError, match=r"\['alpha', 'bravo'\]"):
            _apply_config_overrides({}, {"bravo": 1, "alpha": 2})

    def test_error_points_at_contract_doc(self) -> None:
        with pytest.raises(ValueError, match=r"docs/CONFIG\.md § ModelConfig\.capabilities"):
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


class TestEveryConfigsOverrideKeysAreChecked:
    """No config sets ``temperature`` or ``top_p``, so the refusal cannot come from the
    sampling sweep."""

    @pytest.mark.parametrize(
        ("models", "refused"),
        [
            pytest.param({"agent": _TYPO}, ["models.agent"], id="agent"),
            pytest.param(
                {"agent": {**_MODEL, "fallbacks": [_TYPO]}},
                ["models.agent.fallbacks[0]"],
                id="fallback",
            ),
            pytest.param(
                {"agent": _MODEL, "judge": _TYPO}, ["models.judge"], id="judge-never-built"
            ),
            pytest.param(
                {"agent": _TYPO, "judge": _TYPO}, ["models.agent", "models.judge"], id="two"
            ),
        ],
    )
    def test_validate_and_run_start_refuse_the_same_configs(
        self, tmp_path: Path, models: dict[str, Any], refused: list[str]
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
        assert all("['not_a_key']" in message for _, message in errors)

        run = CliRunner().invoke(cli, ["run", "--config", str(config), "--dry-run"])
        assert run.exit_code == 1, run.output
        refusals = [line for line in run.output.splitlines() if ".capabilities: " in line]
        assert [line.removeprefix("Error: ").split(": ", 1)[0] for line in refusals] == paths

    def test_the_refusal_survives_a_pickle_round_trip(self) -> None:
        err = CapabilityOverrideError(path="models.agent.capabilities", reason="unknown keys")
        back = pickle.loads(pickle.dumps(err))
        assert (type(back), str(back), back.path, back.reason) == (
            CapabilityOverrideError,
            str(err),
            "models.agent.capabilities",
            "unknown keys",
        )
