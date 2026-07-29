"""Keyless shape lock for the ``rag_search`` example pack.

The pack (``examples/native/rag_search/``) drives the first-party rag-service
over ``search_kb`` and grades on a fact that lives only in the knowledge-base
corpus. This guard runs on every PR without Docker or a provider key: it loads
the pack through the same loaders the CLI uses and asserts the load-bearing
shape — the ``search_kb`` tool, the corpus declaration and its non-empty
contents, and the two transcript gates that make grading meaningful. Crucially
it asserts the planted retrieval fact is on **no agent-visible surface**, so the
task cannot be passed without a real retrieval. It is a *shape* lock; the paid
behaviour lock (a graded ``TrialResult`` through the composed stack) lives in
the deploy integration lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import GradingConfig

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "rag_search"
_TASK_YAML = _PACK_ROOT / "dataset" / "tasks" / "kb_lookup_01" / "task.yaml"
_GRADING_YAML = _PACK_ROOT / "dataset" / "tasks" / "kb_lookup_01" / "grading.yaml"

_PLANTED_FACT = "HX49-QORVEN-7731"


def _corpus_files(task_dir: Path, corpus_dir: str) -> list[Path]:
    corpus_path = task_dir / corpus_dir
    return sorted(p for pat in ("*.md", "*.txt") for p in corpus_path.glob(pat))


def test_task_loads_and_enables_search_kb_with_a_corpus() -> None:
    """The task validates, enables ``search_kb``, and declares a non-empty corpus.

    Locks the tool contract and the corpus declaration: the pack retrieves
    through ``search_kb`` (not a browser or bespoke tool), the corpus_dir
    resolves, and it holds documents — so dropping the tool, removing the
    corpus, or emptying it trips CI keylessly.
    """
    assert _TASK_YAML.is_file(), f"pack task.yaml is missing: {_TASK_YAML}"
    task, task_dir = load_task_yaml(_TASK_YAML)

    enabled = task.tools.agent.get("enabled")
    assert enabled == ["search_kb"], f"agent must enable only search_kb, got: {enabled!r}"
    assert task.tools.user.get("enabled") == [], "user must enable no tools"
    # No per-task manifest — the rag-service is reached by DNS on the full stack.
    assert task.environment_manifest is None, "pack must not declare an environment_manifest"

    corpus_dir = (task.initial_state.rag or {}).get("corpus_dir")
    assert corpus_dir == "rag/corpus", f"corpus_dir must be 'rag/corpus', got: {corpus_dir!r}"
    files = _corpus_files(task_dir, corpus_dir)
    assert files, f"corpus at {task_dir / corpus_dir} must be non-empty"


def _load_grading() -> tuple[dict, GradingConfig]:
    raw = yaml.safe_load(_GRADING_YAML.read_text())
    return raw, GradingConfig(**raw)


def test_grading_is_transcript_only_with_both_gates() -> None:
    """Grading validates and locks the two product-scored transcript gates.

    Asserts the planted-fact token, the ``search_kb`` ``required_actions`` gate,
    and that ``combine`` weights only ``transcript_rules`` — so dropping either
    gate, repointing the graded value, or adding a keyed family (llm_judge /
    state_checks) that a keyless run cannot satisfy trips CI without Docker.
    """
    assert _GRADING_YAML.is_file(), f"pack grading.yaml is missing: {_GRADING_YAML}"
    _, grading = _load_grading()

    assert set(grading.combine.weights) == {"transcript_rules"}, (
        "grading must weight only transcript_rules (no llm_judge / state_checks a "
        f"keyless run cannot satisfy), got weights: {grading.combine.weights!r}"
    )
    assert grading.state_checks is None, "grading must not use state_checks"
    assert grading.llm_judge is None, "grading must not use an llm_judge"

    rules = grading.transcript_rules
    assert rules is not None, "grading must define transcript_rules"
    assert _PLANTED_FACT in rules.must_contain, (
        f"grading must require the planted retrieval fact {_PLANTED_FACT!r} in must_contain, "
        f"got: {rules.must_contain!r}"
    )

    kb_gates = [action for action in rules.required_actions if action.name == "search_kb"]
    assert kb_gates, (
        "grading must gate on a search_kb required_action (proves the rag-service "
        f"participated), got: {rules.required_actions!r}"
    )


def test_planted_fact_is_on_no_agent_visible_surface() -> None:
    """The planted fact appears ONLY in the corpus — never where the agent can read it.

    The agent's only channels to the fact are the task text, a system prompt,
    initial filesystem/json_db state, and the ``search_kb`` retrieval. This
    asserts the token is in exactly one corpus doc and on none of the
    agent-visible surfaces, so the task is un-passable without a real retrieval;
    a leak onto any of those surfaces makes it always-passable and fails here.
    """
    task, task_dir = load_task_yaml(_TASK_YAML)

    corpus_hits = [
        f for f in _corpus_files(task_dir, "rag/corpus") if _PLANTED_FACT in f.read_text()
    ]
    assert len(corpus_hits) == 1, (
        f"the planted fact must live in exactly one corpus doc, found it in: "
        f"{[f.name for f in corpus_hits]!r}"
    )

    # The raw task.yaml carries every agent-visible text field (description,
    # initial_user_message, policies, the user backstory) — the token must not
    # be in any of them.
    assert (
        _PLANTED_FACT not in _TASK_YAML.read_text()
    ), "the planted fact leaked into task.yaml — the agent would see it without searching"

    if task.system_prompt is not None:
        prompt_text = (task_dir / task.system_prompt).read_text()
        assert _PLANTED_FACT not in prompt_text, "the planted fact leaked into the system prompt"

    # No agent-visible initial state carries the token either.
    assert not task.initial_state.filesystem, "pack must not seed agent-visible filesystem state"
    assert task.initial_state.json_db is None, "pack must not seed a json_db the agent could read"
