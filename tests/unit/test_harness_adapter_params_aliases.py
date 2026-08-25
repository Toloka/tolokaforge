"""Unit tests for the ``harness_adapter.params.*`` → ``models.agent.*`` aliases.

Two config fields once lived inside the ``terminal_bench``-adapter-specific
``evaluation.harness_adapter.params`` bag:

- ``agent_harness`` (which coding-agent CLI drives the trial)
- ``agent_model`` (the model the CLI receives)

The coding-harness lift makes ``models.agent.harness`` the canonical home for
the first and ``models.agent.name`` the canonical home for the second. This
suite pins the alias behaviour: legacy-only lifts + warns, canonical-only
passes through, both-agree warns once, both-disagree fails loud. Same shape
as ``test_dual_home_aliases.py``.
"""

from __future__ import annotations

import warnings

import pytest

from tolokaforge.core.models import RunConfig

pytestmark = pytest.mark.unit


def _base(**overrides) -> dict:
    """Minimal valid run-config kwargs; callers layer overrides in."""
    return {
        "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
        "orchestrator": {},
        "evaluation": {"output_dir": "results/x"},
        **overrides,
    }


class TestHarnessSelectorAlias:
    def test_legacy_only_lifts_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {
                            "provider": "openrouter",
                            "name": "openrouter/anthropic/claude-sonnet-4-6",
                        },
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {
                            "type": "terminal_bench",
                            "params": {"agent_harness": "claude-code"},
                        },
                    },
                )
            )
        assert cfg.models["agent"].harness == "claude-code"
        # Legacy key is dropped from params after lift.
        assert "agent_harness" not in cfg.evaluation.harness_adapter.params
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "evaluation.harness_adapter.params.agent_harness" in str(w.message)
            for w in caught
        )

    def test_canonical_only_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {
                            "provider": "openrouter",
                            "name": "openrouter/anthropic/claude-sonnet-4-6",
                            "harness": "claude-code",
                        },
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {"type": "terminal_bench", "params": {}},
                    },
                )
            )
        assert cfg.models["agent"].harness == "claude-code"
        assert not any(
            issubclass(w.category, DeprecationWarning) and "agent_harness" in str(w.message)
            for w in caught
        )

    def test_both_agree_warns_once(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {
                            "provider": "openrouter",
                            "name": "openrouter/anthropic/claude-sonnet-4-6",
                            "harness": "claude-code",
                        },
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {
                            "type": "terminal_bench",
                            "params": {"agent_harness": "claude-code"},
                        },
                    },
                )
            )
        assert cfg.models["agent"].harness == "claude-code"
        matches = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "evaluation.harness_adapter.params.agent_harness" in str(w.message)
        ]
        assert len(matches) == 1

    def test_both_disagree_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {
                            "provider": "openrouter",
                            "name": "openrouter/anthropic/claude-sonnet-4-6",
                            "harness": "claude-code",
                        },
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {
                            "type": "terminal_bench",
                            "params": {"agent_harness": "codex"},
                        },
                    },
                )
            )
        msg = str(excinfo.value)
        assert "evaluation.harness_adapter.params.agent_harness" in msg
        assert "models.agent.harness" in msg


class TestAgentModelAlias:
    def test_legacy_only_lifts_and_warns(self) -> None:
        # ``models.agent`` carries ``provider`` (required by ``ModelConfig``);
        # ``name`` is filled from the legacy ``params.agent_model``. The lift
        # deletes the legacy key and emits ``DeprecationWarning``.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {"provider": "openrouter"},
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {
                            "type": "terminal_bench",
                            "params": {
                                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
                            },
                        },
                    },
                )
            )
        assert cfg.models["agent"].name == "openrouter/anthropic/claude-sonnet-4-6"
        assert "agent_model" not in cfg.evaluation.harness_adapter.params
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "evaluation.harness_adapter.params.agent_model" in str(w.message)
            for w in caught
        )

    def test_both_disagree_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            RunConfig(
                **_base(
                    models={
                        "user": {"provider": "openai", "name": "gpt-4o"},
                        "agent": {
                            "provider": "openrouter",
                            "name": "openrouter/anthropic/claude-sonnet-4-6",
                        },
                    },
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {
                            "type": "terminal_bench",
                            "params": {"agent_model": "openrouter/openai/gpt-5.6-sol"},
                        },
                    },
                )
            )
        msg = str(excinfo.value)
        assert "evaluation.harness_adapter.params.agent_model" in msg
        assert "models.agent.name" in msg


class TestModelConfigHarnessField:
    def test_harness_field_accepts_string(self) -> None:
        cfg = RunConfig(
            **_base(
                models={
                    "user": {"provider": "openai", "name": "gpt-4o"},
                    "agent": {
                        "provider": "openrouter",
                        "name": "openrouter/anthropic/claude-sonnet-4-6",
                        "harness": "claude-code",
                    },
                },
            )
        )
        assert cfg.models["agent"].harness == "claude-code"

    def test_harness_field_defaults_to_none(self) -> None:
        cfg = RunConfig(
            **_base(
                models={
                    "user": {"provider": "openai", "name": "gpt-4o"},
                    "agent": {
                        "provider": "openrouter",
                        "name": "openrouter/anthropic/claude-sonnet-4-6",
                    },
                },
            )
        )
        assert cfg.models["agent"].harness is None
