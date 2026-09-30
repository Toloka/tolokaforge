"""``initial_state.rag`` is typed, and its dump carries only what the author wrote (ADR-0052).

``TaskConfig`` is dumped whole by the canonical snapshots, the round-trip paths and any
caller of ``model_dump()``. Were the typed block to dump its defaults, a task that
declares ``corpus_dir`` alone — every rag pack today — would grow ``backend``,
``backend_config`` and a ``tool`` block in every such dump without its author writing
them. The defaults stay readable as attributes; they just never materialise.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import InitialStateConfig, RagConfig, RagToolConfig, TaskConfig
from tolokaforge.core.models.task_config import DEFAULT_SEARCH_TOOL_DESCRIPTION
from tolokaforge.runner.models import DEFAULT_SEARCH_TOOL_NAME, SearchPlane

pytestmark = pytest.mark.unit

_DUMP_MODES: list[dict[str, Any]] = [
    {},
    {"mode": "json"},
    {"mode": "json", "exclude_unset": True},
    {"mode": "json", "exclude_defaults": True},
]


def _task(rag: dict[str, Any] | None) -> TaskConfig:
    return TaskConfig(
        task_id="kb",
        name="kb",
        category="rag_search",
        description="kb",
        max_turns=1,
        initial_user_message="find it",
        initial_state={} if rag is None else {"rag": rag},
        tools={"agent": {"enabled": ["search_kb"]}, "user": {"enabled": []}},
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


@pytest.mark.parametrize("dump", _DUMP_MODES, ids=["python", "json", "unset", "defaults"])
def test_a_corpus_alone_dumps_as_the_corpus_alone(dump: dict[str, Any]) -> None:
    task = _task({"corpus_dir": "rag/corpus"})
    assert task.model_dump(**dump)["initial_state"]["rag"] == {"corpus_dir": "rag/corpus"}


def test_the_defaults_are_read_not_dumped() -> None:
    rag = _task({"corpus_dir": "rag/corpus"}).initial_state.rag
    assert isinstance(rag, RagConfig)
    assert rag.backend == SearchPlane.RAG_SERVICE == "rag_service"
    assert rag.backend_config == {}
    assert rag.tool.name == DEFAULT_SEARCH_TOOL_NAME == "search_kb"
    assert rag.tool.description == (
        "Search the knowledge base for relevant information. Use this to find policies, "
        "procedures, FAQs, and other documentation."
    )
    assert rag.tool.description == DEFAULT_SEARCH_TOOL_DESCRIPTION


def test_a_default_the_author_wrote_out_is_kept() -> None:
    declared = {"corpus_dir": "kb", "backend": "rag_service", "backend_config": {}}
    assert _task(declared).model_dump(mode="json")["initial_state"]["rag"] == declared


def test_a_partial_tool_block_dumps_the_keys_it_declares() -> None:
    declared = {"corpus_dir": "kb", "backend": "bm25", "tool": {"name": "lookup_docs"}}
    rag = _task(declared).initial_state.rag
    assert rag.model_dump(mode="json") == declared
    assert rag.tool.description == DEFAULT_SEARCH_TOOL_DESCRIPTION


def test_the_dump_round_trips() -> None:
    task = _task({"corpus_dir": "kb", "tool": {"description": "Find a policy."}})
    dumped = task.model_dump(mode="json")
    assert TaskConfig(**dumped).model_dump(mode="json") == dumped


def test_a_task_without_a_rag_block_dumps_null() -> None:
    assert _task(None).model_dump(mode="json")["initial_state"]["rag"] is None


def test_an_empty_rag_block_is_a_declaration_with_no_corpus() -> None:
    """``rag: {}`` was a falsy dict; typed it is a model, so readers test ``corpus_dir``."""
    rag = _task({}).initial_state.rag
    assert rag is not None
    assert rag.corpus_dir is None
    assert rag.model_dump() == {}


def test_a_corpus_dir_that_is_not_a_path_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="corpus_dir"):
        InitialStateConfig(rag={"corpus_dir": 42})


@pytest.mark.parametrize(
    ("rag", "loc"),
    [
        ({"corpus_dir": "kb", "backnd": "bm25"}, ("initial_state", "rag", "backnd")),
        ({"corpus_dir": "kb", "tool": {"nme": "lookup"}}, ("initial_state", "rag", "tool", "nme")),
        ({"corpus_dir": "kb", "tool": None}, ("initial_state", "rag", "tool")),
    ],
    ids=["misspelt-backend", "misspelt-tool-name", "null-tool"],
)
def test_a_misspelt_or_malformed_key_is_refused_not_defaulted(
    rag: dict[str, Any], loc: tuple[str, ...]
) -> None:
    """A typo would otherwise select the default backend or tool without a word."""
    with pytest.raises(ValidationError) as excinfo:
        _task(rag)
    assert [tuple(detail["loc"]) for detail in excinfo.value.errors()] == [loc]


def test_a_mapping_stored_without_validation_dumps_as_the_mapping(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """``model_copy(update=…)`` stores the value unvalidated; the dump must not raise."""
    state = InitialStateConfig().model_copy(update={"rag": {"corpus_dir": "kb", "extra": 1}})
    assert state.model_dump(mode="json")["rag"] == {"corpus_dir": "kb", "extra": 1}
    assert state.model_dump()["rag"] == {"corpus_dir": "kb", "extra": 1}


def test_there_is_no_tool_actors_field() -> None:
    """The actor that gets the tool is ``tools.<actor>.enabled``, declared once."""
    assert set(RagToolConfig.model_fields) == {"name", "description"}
    assert set(RagConfig.model_fields) == {"corpus_dir", "backend", "backend_config", "tool"}
