"""Canonical dry-run test for judge-kind-ab's runner + report plumbing.

Drives ``run_live_ab`` and ``write_report`` against a small, locally-built
corpus with :class:`ScriptedLLMClient`-backed cassette providers instead of a
live ``LiteLLMJudgeModelProvider`` — proves the plumbing renders a well-formed
per-criterion kappa + cost report with ZERO real LLM calls. Cassette shape and
provider-factory wiring mirror ``tests/canonical/test_judge_kind_parity.py``,
reimplemented here (not cross-imported) since ``tools/`` workspace members do
not import from ``tests/`` outside their own tree, other than the shared
``ScriptedLLMClient`` test double.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from judge_kind_ab.report import write_report
from judge_kind_ab.runner import run_live_ab

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds.parity import ParityCorpusEntry
from tolokaforge.core.grading.judge_model_provider import JudgeModel, JudgeModelProvider
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Rubric

pytestmark = pytest.mark.canonical

_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)
_KIND_NAMES = ("single_shot_rubric",)
_RUBRIC = Rubric.model_validate(
    {
        "criteria": [
            {
                "id": "criterion_a",
                "description": "A is satisfied.",
                "kind": "binary",
                "weight": 1.0,
            },
            {
                "id": "criterion_b",
                "description": "B is satisfied.",
                "kind": "binary",
                "weight": 1.0,
            },
        ]
    }
)


def _verdict_marker(met: bool) -> str:
    return "VERDICT: MET" if met else "VERDICT: NOT MET"


def _submit_report_step(
    entry_id: str, *, met_a: bool, met_b: bool
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "submit_report",
            {
                "reasons": f"deterministic dry-run verdict for {entry_id}",
                "criterion_a": met_a,
                "criterion_a_justification": f"criterion_a — dry-run\n{_verdict_marker(met_a)}",
                "criterion_b": met_b,
                "criterion_b_justification": f"criterion_b — dry-run\n{_verdict_marker(met_b)}",
            },
        )
    ]


def _entry(entry_id: str, *, met_a: bool, met_b: bool) -> ParityCorpusEntry:
    script = [_submit_report_step(entry_id, met_a=met_a, met_b=met_b)]
    return ParityCorpusEntry(
        entry_id=entry_id,
        rubric=_RUBRIC,
        agent_system_prompt=f"You are a task agent. ({entry_id})",
        transcript=[{"role": "user", "content": f"Do the task. ({entry_id})"}],
        state_diff=None,
        judge_scripts={"single_shot_rubric": script},
    )


def _corpus() -> list[tuple[str, ParityCorpusEntry]]:
    return [
        ("family_a", _entry("family_a_entry_01", met_a=True, met_b=False)),
        ("family_a", _entry("family_a_entry_02", met_a=True, met_b=False)),
        ("family_b", _entry("family_b_entry_01", met_a=False, met_b=True)),
    ]


def _scripted_provider_factory_for(entries: list[ParityCorpusEntry], kind_name: str):
    """Local reimplementation of the parity lane's per-replay cassette pool."""

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining = [ScriptedLLMClient(list(entry.judge_scripts[kind_name])) for entry in entries]

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def test_run_live_ab_dry_run_renders_well_formed_report(tmp_path: Path) -> None:
    corpus = _corpus()
    entries = [entry for _family, entry in corpus]

    def _provider_factory_for(name: str):
        return _scripted_provider_factory_for(entries, name)

    result = run_live_ab(
        corpus,
        kind_names=_KIND_NAMES,
        replays=2,
        judge_model_config=_JUDGE_MODEL,
        provider_factory_for=_provider_factory_for,
    )

    assert result.cross_kind == ()

    assert {s.kind for s in result.self_consistency} == set(_KIND_NAMES)
    for self_result in result.self_consistency:
        for verdict in self_result.decision.per_criterion:
            assert verdict.kappa == pytest.approx(1.0), "deterministic replays must agree perfectly"

    for name in _KIND_NAMES:
        usage = result.usage_by_kind[name]
        assert len(usage) > 0
        assert sum(record.usage.calls for record in usage) > 0
        assert {record.family for record in usage} == {"family_a", "family_b"}

    write_report(result, tmp_path)
    report_md = (tmp_path / "report.md").read_text()
    report_json = json.loads((tmp_path / "report.json").read_text())

    assert "## Cost by kind" in report_md
    assert "## Cost by kind and task family" in report_md
    for name in _KIND_NAMES:
        assert name in report_md

    assert report_json["cross_kind"] == []
    assert len(report_json["self_consistency"]) == 1
    for name in _KIND_NAMES:
        assert report_json["usage_by_kind"][name]["total"]["calls"] > 0
        assert set(report_json["usage_by_kind"][name]["by_family"]) == {"family_a", "family_b"}
