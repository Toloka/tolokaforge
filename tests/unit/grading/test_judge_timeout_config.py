"""Resolution of the judge episode budget: env → config → default.

The judge episode timeout (:data:`DEFAULT_JUDGE_EPISODE_TIMEOUT_S`) and
turn cap (:data:`DEFAULT_JUDGE_MAX_TURNS`) are configurable through
:class:`~tolokaforge.runner.models.LLMJudgeConfig` (per-task) and
:class:`~tolokaforge.core.models.task_config.LLMJudgeDefaults`
(per-project). The episode timeout ALSO reads env var
``TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S`` for eval-side overrides that
must not require a pack edit — env wins over config wins over default.

The turn cap has no env override: the cap protects against a looping
judge and belongs on a config-level knob, not an eval-side lever.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge import (
    DEFAULT_JUDGE_EPISODE_TIMEOUT_S,
    DEFAULT_JUDGE_MAX_TURNS,
    _resolve_judge_episode_timeout,
    _resolve_judge_max_turns,
)

pytestmark = pytest.mark.unit


class TestResolveJudgeEpisodeTimeout:
    """``TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S`` → configured → default."""

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", raising=False)
        assert _resolve_judge_episode_timeout(None) == float(DEFAULT_JUDGE_EPISODE_TIMEOUT_S)

    def test_configured_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", raising=False)
        assert _resolve_judge_episode_timeout(480.0) == 480.0

    def test_configured_int_is_coerced_to_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", raising=False)
        assert _resolve_judge_episode_timeout(600) == 600.0

    def test_env_wins_over_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", "720")
        assert _resolve_judge_episode_timeout(480.0) == 720.0

    def test_env_wins_over_default_when_configured_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", "300")
        assert _resolve_judge_episode_timeout(None) == 300.0


class TestResolveJudgeMaxTurns:
    """Turn cap: configured → default. No env override."""

    def test_returns_default_when_unset(self) -> None:
        assert _resolve_judge_max_turns(None) == DEFAULT_JUDGE_MAX_TURNS

    def test_configured_wins_over_default(self) -> None:
        assert _resolve_judge_max_turns(20) == 20

    def test_env_var_does_not_override_max_turns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env-side overrides never touch the turn cap — only the wall-time
        budget. A stray env with the same shape leaves the configured turn
        cap in effect."""
        monkeypatch.setenv("TOLOKAFORGE_JUDGE_MAX_TURNS", "99")
        assert _resolve_judge_max_turns(14) == 14


class TestLLMJudgeConstructionResolvesBudget:
    """``LLMJudge.__init__`` composes the two resolvers when it takes the
    kwargs from :class:`LLMJudgeConfig` or from the wire replay path."""

    def test_llmjudge_stores_resolved_defaults_when_kwargs_are_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tolokaforge.core.grading.judge import LLMJudge
        from tolokaforge.core.models import ModelConfig

        monkeypatch.delenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", raising=False)
        judge = LLMJudge(ModelConfig(provider="openai", name="gpt-4o-mini"))
        assert judge._episode_timeout_s == float(DEFAULT_JUDGE_EPISODE_TIMEOUT_S)
        assert judge._max_turns == DEFAULT_JUDGE_MAX_TURNS

    def test_llmjudge_stores_per_config_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tolokaforge.core.grading.judge import LLMJudge
        from tolokaforge.core.models import ModelConfig

        monkeypatch.delenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", raising=False)
        judge = LLMJudge(
            ModelConfig(provider="openai", name="gpt-4o-mini"),
            episode_timeout_s=480.0,
            max_turns=20,
        )
        assert judge._episode_timeout_s == 480.0
        assert judge._max_turns == 20

    def test_llmjudge_env_override_wins_over_per_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tolokaforge.core.grading.judge import LLMJudge
        from tolokaforge.core.models import ModelConfig

        monkeypatch.setenv("TOLOKAFORGE_JUDGE_EPISODE_TIMEOUT_S", "600")
        judge = LLMJudge(
            ModelConfig(provider="openai", name="gpt-4o-mini"),
            episode_timeout_s=480.0,
        )
        assert judge._episode_timeout_s == 600.0

    def test_llmjudge_config_field_defaults_to_none(self) -> None:
        """The wire-side :class:`LLMJudgeConfig` leaves both fields ``None``
        by default, so a pack that never authors either lands on the
        engine defaults at construction."""
        from tolokaforge.core.grading.judge_kinds.single_shot import (
            SingleShotRubricJudgeKind,  # noqa: F401
        )
        from tolokaforge.runner.models import Criterion, LLMJudgeConfig, Rubric

        config = LLMJudgeConfig(
            rubric=Rubric(criteria=[Criterion(id="c1", description="d", weight=1.0, kind="binary")])
        )
        assert config.episode_timeout_s is None
        assert config.max_turns is None

    def test_llmjudge_config_rejects_zero_and_negative_budgets(self) -> None:
        """A wall-time budget must be positive; a turn cap must be at
        least one. Pydantic surfaces these at load, not at judge time."""
        from pydantic import ValidationError

        from tolokaforge.runner.models import Criterion, LLMJudgeConfig, Rubric

        rubric = Rubric(criteria=[Criterion(id="c1", description="d", weight=1.0, kind="binary")])
        with pytest.raises(ValidationError):
            LLMJudgeConfig(rubric=rubric, episode_timeout_s=0.0)
        with pytest.raises(ValidationError):
            LLMJudgeConfig(rubric=rubric, episode_timeout_s=-5.0)
        with pytest.raises(ValidationError):
            LLMJudgeConfig(rubric=rubric, max_turns=0)
        with pytest.raises(ValidationError):
            LLMJudgeConfig(rubric=rubric, max_turns=-1)
