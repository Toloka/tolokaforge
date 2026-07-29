"""Keyless shape lock for the native RAG cross-stage contract.

The ``TaskDescription`` a native RAG pack serializes is the wire the runner
consumes to index the per-trial corpus (issue #107). This locks that contract at
the serialization boundary — the shape Stage 2's runner resolves against:

- ``search.enabled`` is ``True`` with ``documents_path`` equal to the pack's
  declared ``corpus_dir`` **verbatim** (the pinned convention the runner resolves
  as ``artifacts_dir / documents_path``);
- the corpus ``.md``/``.txt`` files ride in ``tool_artifacts`` under that same
  ``corpus_dir`` prefix, and nothing else does (never ``grading.yaml`` — it can
  carry the planted retrieval fact);
- the ``search_kb`` agent tool advertises the real ``{query, top_k, alpha}``
  schema, source-less so the runner reconstructs it as a RAG wrapper.

Keyless — trips on adapter rot without Docker or a provider key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.canonical

_PLANTED_FACT = "Warehouse SKU QX-4410 ships in refrigerated crates only."


def _write_rag_pack(tmp_path: Path) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "rag_shape"
    corpus_dir = task_dir / "rag" / "corpus"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "handbook.md").write_text(f"# Handbook\n\n{_PLANTED_FACT}\n")
    (corpus_dir / "notes.txt").write_text("Cold-chain logistics notes.\n")
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "rag_shape",
            "name": "rag shape",
            "category": "rag_search",
            "description": "rag shape lock",
            "initial_state": {"json_db": "initial_state.json", "rag": {"corpus_dir": "rag/corpus"}},
            "tools": {
                "agent": {"enabled": ["search_kb", "submit_report"]},
                "user": {"enabled": []},
            },
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
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


def test_native_rag_pack_serializes_search_and_corpus(tmp_path: Path) -> None:
    adapter = _write_rag_pack(tmp_path)
    payload = adapter.to_task_description("rag_shape").model_dump(mode="json")

    assert payload["search"]["enabled"] is True
    assert payload["search"]["documents_path"] == "rag/corpus"
    assert payload["search"]["domain_name"] == "rag_search"

    artifacts = payload["tool_artifacts"]
    assert set(artifacts) == {"rag/corpus/handbook.md", "rag/corpus/notes.txt"}

    search_kb = next(t for t in payload["agent_tools"] if t["name"] == "search_kb")
    assert search_kb["source"] is None
    assert search_kb["parameters"]["required"] == ["query"]
    assert set(search_kb["parameters"]["properties"]) == {"query", "top_k", "alpha"}
