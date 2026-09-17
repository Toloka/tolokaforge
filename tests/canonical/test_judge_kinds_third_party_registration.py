"""``tolokaforge.judge_kinds`` — third-party ``importlib.metadata`` registration lock.

A downstream package registering a class under the
``tolokaforge.judge_kinds`` entry-point group MUST resolve through
:func:`load_judge_kind` without a framework PR. This test injects a
fake :class:`JudgeKind` class into the discovery scan via
``importlib.metadata.entry_points`` monkeypatching (mirrors
:func:`test_rubric_evaluator` fixture pattern in
:mod:`tests.canonical.test_grading_rubric_evaluator_dispatch`), then
resolves it and dispatches ``.evaluate(...)``, asserting the sentinel
:class:`JudgeResult` the fake kind emits.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any, ClassVar

import pytest

from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.plugin_registry import (
    JUDGE_KINDS_GROUP,
    _clear_discovery_cache,
    load_judge_kind,
)
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

pytestmark = pytest.mark.canonical


class _FakeJudgeKind:
    """Deterministic third-party judge kind for the registration test."""

    NAME: ClassVar[str] = "test_judge_kind"

    def evaluate(self, **kwargs: Any) -> JudgeResult:  # noqa: ARG002 — sentinel ignores every kwarg
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons="third-party fake kind reached",
            score=1.0,
            criterion_results=(
                CriterionResult(
                    id="sentinel",
                    met=True,
                    score=1.0,
                    justification="deterministic sentinel",
                ),
            ),
        )


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan.

    Enumerates ``name`` / ``dist`` and returns ``value`` on ``load()`` —
    the surface :func:`discover_entry_points` reads.
    """

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


def _inject_fake_judge_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fake judge kind alongside the shipped ``single_shot_rubric``."""
    _clear_discovery_cache()
    shipped = list(importlib.metadata.entry_points(group=JUDGE_KINDS_GROUP))
    injected = _EntryPointStub("test_judge_kind", _FakeJudgeKind)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == JUDGE_KINDS_GROUP:
            return [*shipped, injected]
        return list(importlib.metadata.entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()


def test_third_party_judge_kind_registration_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_fake_judge_kind(monkeypatch)
    try:
        cls = load_judge_kind("test_judge_kind")
        assert cls is _FakeJudgeKind
        result = cls().evaluate(
            rubric=Rubric(
                criteria=[Criterion(id="sentinel", description="anything", kind="binary")]
            ),
            agent_system_prompt="",
            transcript=[],
            db_reader=None,
            kb_search=None,
            workspace_dir=None,
            extra_read_tools=[],
            state_diff=None,
            judge_model_config=None,
            judge_model_provider=None,
            disable_knowledge_search=False,
            custom_system_prompt=None,
            include_agent_system_prompt=True,
            kind_config=None,
            logger=None,
        )
        assert result.status is JudgeStatus.COMPLETED
        assert result.reasons == "third-party fake kind reached"
        assert [cr.id for cr in result.criterion_results] == ["sentinel"]
    finally:
        _clear_discovery_cache()
