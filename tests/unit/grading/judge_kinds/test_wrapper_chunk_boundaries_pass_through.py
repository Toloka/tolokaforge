"""Wrapper kinds must NOT erase the wrapped kind's ``chunk_boundaries``.

Locks the composition path the PR ships as a headline feature: when a wrapper
(``voted_rubric`` / ``jury_rubric``) sits above a chunking kind
(``chunked_rubric``), the outer ``JudgeResult.chunk_boundaries`` must carry
the same boundary shape the inner chunking kind produced. Downstream
offline-replay reads this field to route retries by chunk; erasing it in the
wrapper would strip that signal on every composed configuration and silently
break the audit/persistence contract on the composition the PR advertises.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds.chunked import ChunkedRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.voted import VotedRubricJudgeKind
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)

    def build(self, model_config: ModelConfig):  # noqa: ARG002
        return self._clients.pop(0)


def _chunk_submit_call(criteria: list[Criterion]) -> list:
    args: dict[str, Any] = {"reasons": "overall"}
    for c in criteria:
        args[c.id] = True
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: MET"
    return [("submit_report", args)]


def _rubric_forced_to_chunk_at_size_2() -> Rubric:
    """4 criteria at ``chunk_size=2`` → 2 chunks. Small enough to script cheaply."""
    return Rubric(
        criteria=[
            Criterion(id="c0", description="c0"),
            Criterion(id="c1", description="c1"),
            Criterion(id="c2", description="c2"),
            Criterion(id="c3", description="c3"),
        ]
    )


def _evaluate_kwargs(rubric: Rubric, provider: _QueuedProvider, kind_config):
    return {
        "rubric": rubric,
        "agent_system_prompt": "you are an agent",
        "transcript": [{"role": "user", "content": "hi"}],
        "db_reader": None,
        "kb_search": None,
        "workspace_dir": None,
        "extra_read_tools": [],
        "state_diff": None,
        "judge_model_config": _JUDGE_MODEL,
        "judge_model_provider": provider,
        "disable_knowledge_search": False,
        "custom_system_prompt": None,
        "include_agent_system_prompt": True,
        "kind_config": kind_config,
        "logger": StructuredLogger(name="test-wrapper-chunk-boundaries"),
    }


def test_voted_wrapping_chunked_preserves_chunk_boundaries(monkeypatch) -> None:
    """voted+chunked must carry the inner chunk_boundaries out on the wrapper's result.

    voted requires K>=2. Each sample runs chunked at chunk_size=2 over 4
    criteria = 2 judge calls per sample × 2 samples = 4 scripted responses.
    """
    rubric = _rubric_forced_to_chunk_at_size_2()
    # Two samples × 2 chunks = 4 scripted judge calls.
    scripts = [
        [_chunk_submit_call(rubric.criteria[0:2])],
        [_chunk_submit_call(rubric.criteria[2:4])],
        [_chunk_submit_call(rubric.criteria[0:2])],
        [_chunk_submit_call(rubric.criteria[2:4])],
    ]
    provider = _QueuedProvider([ScriptedLLMClient(script) for script in scripts])

    # Patch load_judge_kind so voted routes wrapped_kind="chunked_rubric"
    # without depending on entry-point registration of chunked_rubric's real
    # name (voted's load_judge_kind call happens inside its evaluate).
    from tolokaforge.core import plugin_registry

    real_load = plugin_registry.load_judge_kind

    def _fake_load(name: str):
        if name == "chunked_rubric":
            return ChunkedRubricJudgeKind
        return real_load(name)

    monkeypatch.setattr(plugin_registry, "load_judge_kind", _fake_load)

    result = VotedRubricJudgeKind().evaluate(
        **_evaluate_kwargs(
            rubric,
            provider,
            kind_config={
                "n_samples": 2,
                "aggregator": "median",
                "wrapped_kind": "chunked_rubric",
                "wrapped_kind_config": {"chunk_size": 2},
            },
        )
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.chunk_boundaries == (("c0", "c1"), ("c2", "c3"))


def test_voted_wrapping_singleshot_carries_empty_chunk_boundaries() -> None:
    """When the wrapped kind is a non-chunking kind, chunk_boundaries stays empty."""
    rubric = Rubric(criteria=[Criterion(id="c0", description="c0")])

    class _StubKind:
        NAME = "stub"

        def evaluate(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(
                status=JudgeStatus.COMPLETED,
                usage=JudgeUsage(),
                reasons="ok",
                score=1.0,
                binary_pass=True,
                criterion_results=(),
                chunk_boundaries=(),
            )

    from unittest.mock import patch

    from tolokaforge.core import plugin_registry

    with patch.object(plugin_registry, "load_judge_kind", lambda name: _StubKind):
        provider = _QueuedProvider([])
        result = VotedRubricJudgeKind().evaluate(
            **_evaluate_kwargs(
                rubric,
                provider,
                kind_config={
                    "n_samples": 2,
                    "aggregator": "median",
                    "wrapped_kind": "stub",
                },
            )
        )
    assert result.chunk_boundaries == ()
