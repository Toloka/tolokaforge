"""``NativeAdapter.to_task_description`` populates ``SearchConfig`` and
bundles the RAG corpus from ``initial_state.rag``.

A native task that declares ``initial_state.rag.corpus_dir`` and enables the
``search_kb`` agent tool gets per-trial RAG indexing: ``search.enabled`` flips
to ``True``, the corpus files travel in ``tool_artifacts`` under the declared
``corpus_dir`` prefix (and nothing else — never ``task.yaml``/``grading.yaml``),
and the ``search_kb`` agent tool carries the real ``{query, top_k, alpha}``
schema. Tasks with no corpus keep search disabled and ship no corpus artifacts.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit

_PLANTED_FACT = "Refund policy code RX-7788 permits a 45-day window."


def _build_adapter(
    tmp_path: Path,
    *,
    enabled_agent_tools: list[str],
    enabled_user_tools: list[str] | None = None,
    rag: dict | None = None,
    write_corpus: bool = True,
) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "rag_task"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    if write_corpus:
        corpus_dir = task_dir / "rag" / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "policies.md").write_text(f"# Policies\n\n{_PLANTED_FACT}\n")
        (corpus_dir / "faq.txt").write_text("Q: What is the return window?\n")
    task_yaml: dict = {
        "task_id": "rag_task",
        "name": "rag task",
        "category": "rag_search",
        "description": "rag task",
        "initial_state": {"json_db": "initial_state.json"},
        "tools": {
            "agent": {"enabled": enabled_agent_tools},
            "user": {"enabled": enabled_user_tools or []},
        },
        "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
        "grading": "grading.yaml",
        "system_prompt": "system_prompt.md",
    }
    if rag is not None:
        task_yaml["initial_state"]["rag"] = rag
    write_yaml_file(task_dir / "task.yaml", task_yaml)
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


class TestRagSearchEnabled:
    def test_corpus_and_search_kb_enable_per_trial_rag(self, tmp_path: Path) -> None:
        adapter = _build_adapter(
            tmp_path,
            enabled_agent_tools=["search_kb", "bash"],
            rag={"corpus_dir": "rag/corpus"},
        )
        td = adapter.to_task_description("rag_task")

        assert td.search.enabled is True
        assert td.search.domain_name == "rag_search"
        assert td.search.documents_path == "rag/corpus"

        # Corpus files travel under the declared corpus_dir prefix, decoded
        # back to their on-disk content.
        assert "rag/corpus/policies.md" in td.tool_artifacts
        assert "rag/corpus/faq.txt" in td.tool_artifacts
        decoded = base64.b64decode(td.tool_artifacts["rag/corpus/policies.md"]).decode()
        assert _PLANTED_FACT in decoded

        # Only the corpus travels — never the task/grading yaml (the latter
        # can carry a planted retrieval fact).
        assert "task.yaml" not in td.tool_artifacts
        assert "grading.yaml" not in td.tool_artifacts

        # search_kb carries the real schema, not a parameter-less stub.
        search_kb = next(t for t in td.agent_tools if t.name == "search_kb")
        assert search_kb.parameters["required"] == ["query"]
        assert set(search_kb.parameters["properties"]) == {"query", "top_k", "alpha"}

    def test_a_user_declared_search_kb_searches_the_same_corpus(self, tmp_path: Path) -> None:
        """The tool the corpus is for may be the user simulator's.

        ``search_kb`` is rebuilt as the same source-less RAG wrapper whichever
        actor declares it, so a corpus declared beside a user-side ``search_kb``
        is searchable and the trial needs its index — reading the agent's block
        alone would refuse the pack for a corpus nobody could search.
        """
        adapter = _build_adapter(
            tmp_path,
            enabled_agent_tools=["bash"],
            enabled_user_tools=["search_kb"],
            rag={"corpus_dir": "rag/corpus"},
        )
        td = adapter.to_task_description("rag_task")

        assert td.search.enabled is True
        assert td.search.documents_path == "rag/corpus"
        assert "rag/corpus/policies.md" in td.tool_artifacts
        assert [t.name for t in td.agent_tools] == ["bash"]
        search_kb = next(t for t in td.user_tools if t.name == "search_kb")
        assert set(search_kb.parameters["properties"]) == {"query", "top_k", "alpha"}


class TestRagSearchDisabled:
    def test_no_rag_block_keeps_search_disabled(self, tmp_path: Path) -> None:
        adapter = _build_adapter(tmp_path, enabled_agent_tools=["bash"])
        td = adapter.to_task_description("rag_task")

        assert td.search.enabled is False
        assert td.search.domain_name is None
        assert td.search.documents_path is None
        assert td.tool_artifacts == {}


class TestRagSearchFailFast:
    def test_corpus_without_search_kb_raises(self, tmp_path: Path) -> None:
        adapter = _build_adapter(
            tmp_path,
            enabled_agent_tools=["submit_report"],
            rag={"corpus_dir": "rag/corpus"},
        )
        with pytest.raises(ValueError, match="search_kb"):
            adapter.to_task_description("rag_task")

    def test_corpus_dir_that_does_not_resolve_raises(self, tmp_path: Path) -> None:
        adapter = _build_adapter(
            tmp_path,
            enabled_agent_tools=["search_kb"],
            rag={"corpus_dir": "rag/missing"},
        )
        with pytest.raises(ValueError, match="not a directory"):
            adapter.to_task_description("rag_task")
