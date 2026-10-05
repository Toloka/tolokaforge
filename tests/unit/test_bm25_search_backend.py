"""The ``bm25`` search backend: its config, its corpus, its ranking, its renderings.

Every expectation is behavioural: what the agent reads, what the judge gets, what
the schema exposes, and what is refused — never the implementation. The scorer's
arithmetic is pinned separately in ``test_bm25_okapi_parity.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit
from tolokaforge.core.plugin_registry import load_search_backend
from tolokaforge.core.search.backend import (
    SearchBackend,
    SearchBackendContext,
    SearchIndex,
    SearchIndexBuildError,
    SearchOutcome,
)
from tolokaforge.core.search.bm25 import (
    TOKENIZERS,
    Bm25BackendConfig,
    Bm25CorpusError,
    Bm25DocumentsConfig,
    Bm25JsonRender,
    Bm25KnowledgeSearch,
    Bm25SearchBackend,
    Bm25SearchIndex,
    Bm25TextRender,
    OkapiBm25,
    clear_index_cache,
    load_bm25_corpus,
)
from tolokaforge.runner.models import SearchPlane

pytestmark = pytest.mark.unit

TRIAL_ID = "bm25_task:0"
_LOGGER = logging.getLogger("tests.bm25")


def _context(config: dict[str, Any] | None = None, *, trial_id: str | None = TRIAL_ID):
    return SearchBackendContext(
        backend_config=config or {},
        tool_name="search_kb",
        tool_description="Search the knowledge base.",
        logger=_LOGGER,
        trial_id=trial_id,
        domain_name="synthetic",
    )


def _backend(config: dict[str, Any] | None = None, **context: Any) -> Bm25SearchBackend:
    return Bm25SearchBackend(_context(config, **context))


def _write(directory: Path, files: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _json_doc(doc_id: str, title: str, content: str) -> dict[str, str]:
    return {"id": doc_id, "title": title, "content": content}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_index_cache()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Three JSON documents and one text document, loaded in file-name order."""
    return _write(
        tmp_path / "kb",
        {
            "a_returns.json": _json_doc("ret-1", "Returns", "refund window thirty days"),
            "b_shipping.json": _json_doc("ship-1", "Shipping", "orders ship in two days"),
            "c_warranty.json": _json_doc("war-1", "Warranty", "one year warranty on hardware"),
            "d_notes.md": "plain notes about shipping and warranty",
        },
    )


def _index(backend: Bm25SearchBackend, corpus_dir: Path) -> Bm25SearchIndex:
    return asyncio.run(backend.build_index(corpus_dir))


def _search(
    index: Bm25SearchIndex, query: str, arguments: dict[str, Any] | None = None
) -> SearchOutcome:
    call = {"query": query, **(arguments or {})}
    return asyncio.run(index.search(query, call, budget_s=15.0))


# =============================================================================
# The config model
# =============================================================================


class TestBackendConfig:
    def test_every_key_has_the_documented_default(self) -> None:
        config = Bm25BackendConfig()
        assert config.documents.model_dump() == {
            "format": "auto",
            "order": "filename",
            "skip_prefix": "_",
            "fields": ["content"],
        }
        assert config.tokenizer == "whitespace_lower"
        assert config.bm25.model_dump() == {"k1": 1.5, "b": 0.75, "epsilon": 0.25}
        assert config.ranking.model_dump() == {
            "top_k": 5,
            "min_score": None,
            "tie_break": "corpus_order",
        }
        assert config.empty_query == "no_results"
        assert isinstance(config.render, Bm25JsonRender)
        assert config.agent_parameters == ["query"]

    @pytest.mark.parametrize(
        "config",
        [
            {"tokeniser": "whitespace_lower"},
            {"documents": {"formats": "json"}},
            {"bm25": {"k": 1.0}},
            {"ranking": {"topk": 3}},
            {"render": {"kind": "text", "template": "{id}"}},
        ],
        ids=["top-level", "documents", "bm25", "ranking", "render"],
    )
    def test_an_unknown_key_is_refused(self, config: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="refused backend_config"):
            _backend(config)

    @pytest.mark.parametrize(
        ("config", "reason"),
        [
            ({"tokenizer": "stemmer"}, "unknown tokenizer"),
            ({"agent_parameters": ["top_k"]}, "must include 'query'"),
            ({"agent_parameters": ["query", "query"]}, "repeats"),
            ({"agent_parameters": ["query", "alpha"]}, "alpha"),
            ({"documents": {"fields": []}}, "at least one field"),
            ({"documents": {"fields": ["content", "content"]}}, "repeats"),
            ({"documents": {"format": "yaml"}}, "yaml"),
            ({"documents": {"order": "random"}}, "random"),
            ({"ranking": {"top_k": 0}}, "top_k"),
            ({"ranking": {"tie_break": "score_then_id"}}, "tie_break"),
            ({"bm25": {"b": 1.5}}, "less than or equal to 1"),
            ({"empty_query": "ignore"}, "empty_query"),
            ({"render": {"kind": "xml"}}, "kind"),
            ({"render": {"kind": "text", "item_template": "{rank}. {title}"}}, "unknown fields"),
            ({"render": {"kind": "text", "item_template": "{}"}}, "positional"),
            ({"render": {"kind": "text", "timing_template": "{elapsed}"}}, "unknown fields"),
            ({"render": {"kind": "text", "score_format": "zz"}}, "cannot format a float"),
        ],
    )
    def test_a_value_outside_the_contract_is_refused_naming_it(
        self, config: dict[str, Any], reason: str
    ) -> None:
        with pytest.raises(ValueError, match=reason):
            _backend(config)

    def test_the_refusal_names_the_backend(self) -> None:
        with pytest.raises(ValueError, match="search backend 'bm25' refused backend_config"):
            _backend({"ranking": {"top_k": -1}})

    def test_the_text_renderer_takes_a_format_spec_on_a_field(self) -> None:
        backend = _backend({"render": {"kind": "text", "item_template": "{id}: {content:.5}"}})
        assert isinstance(backend.config.render, Bm25TextRender)

    def test_the_backend_is_the_registered_built_in(self) -> None:
        factory = load_search_backend("bm25")
        backend = factory(_context())
        assert isinstance(backend, Bm25SearchBackend)
        assert isinstance(backend, SearchBackend)
        assert backend.name == SearchPlane.BM25.value == "bm25"
        assert backend.stack_service is None

    def test_the_backend_builds_from_a_trial_less_context(self) -> None:
        backend = _backend({"ranking": {"top_k": 2}}, trial_id=None)
        assert backend.tool_parameters()["required"] == ["query"]


# =============================================================================
# The corpus
# =============================================================================


class TestCorpusLoading:
    def test_documents_load_in_file_name_order_with_ids_and_titles(self, corpus: Path) -> None:
        documents = load_bm25_corpus(corpus, Bm25DocumentsConfig())
        assert [(d.id, d.title, d.source) for d in documents] == [
            ("ret-1", "Returns", "a_returns.json"),
            ("ship-1", "Shipping", "b_shipping.json"),
            ("war-1", "Warranty", "c_warranty.json"),
            ("d_notes", "d_notes", "d_notes.md"),
        ]
        assert documents[3].content == "plain notes about shipping and warranty"

    def test_order_is_by_file_name_not_by_creation(self, tmp_path: Path) -> None:
        directory = _write(tmp_path / "kb", {"zed.txt": "last", "alpha.txt": "first"})
        documents = load_bm25_corpus(directory, Bm25DocumentsConfig())
        assert [d.id for d in documents] == ["alpha", "zed"]

    def test_files_under_the_skip_prefix_are_not_documents(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb", {"_README.md": "about this corpus", "doc.md": "the document"}
        )
        assert [d.id for d in load_bm25_corpus(directory, Bm25DocumentsConfig())] == ["doc"]
        everything = Bm25DocumentsConfig(skip_prefix="")
        assert [d.id for d in load_bm25_corpus(directory, everything)] == ["_README", "doc"]
        other = Bm25DocumentsConfig(skip_prefix="doc")
        assert [d.id for d in load_bm25_corpus(directory, other)] == ["_README"]

    def test_format_json_reads_json_files_only_and_text_reads_text_only(self, corpus: Path) -> None:
        json_only = load_bm25_corpus(corpus, Bm25DocumentsConfig(format="json"))
        assert [d.id for d in json_only] == ["ret-1", "ship-1", "war-1"]
        text_only = load_bm25_corpus(corpus, Bm25DocumentsConfig(format="text"))
        assert [d.id for d in text_only] == ["d_notes"]

    def test_files_of_other_extensions_and_subdirectories_are_not_documents(
        self, tmp_path: Path
    ) -> None:
        directory = _write(tmp_path / "kb", {"doc.txt": "text", "data.yaml": "a: 1"})
        (directory / "nested").mkdir()
        (directory / "nested" / "inner.md").write_text("nested")
        assert [d.id for d in load_bm25_corpus(directory, Bm25DocumentsConfig())] == ["doc"]

    @pytest.mark.parametrize(
        ("files", "reason"),
        [
            (
                {"a.json": _json_doc("x", "A", "a"), "b.json": _json_doc("x", "B", "b")},
                "already used",
            ),
            ({"x.md": "md", "x.txt": "txt"}, "already used"),
            ({"blank.md": "   \n"}, "has no content"),
            ({"blank.json": _json_doc("b", "Blank", " ")}, "has no content"),
            ({"missing.json": {"id": "m", "title": "M"}}, "missing \\['content'\\]"),
            ({"typed.json": {"id": 7, "title": "T", "content": "c"}}, "'id' is int"),
            ({"noid.json": _json_doc("  ", "T", "c")}, "'id' is blank"),
            ({"list.json": [_json_doc("a", "A", "a")]}, "one object"),
            ({"broken.json": "{not json"}, "not a JSON document"),
        ],
        ids=[
            "duplicate-json-id",
            "duplicate-stem",
            "blank-text",
            "blank-json",
            "missing-key",
            "non-string",
            "blank-id",
            "list-not-object",
            "malformed-json",
        ],
    )
    def test_a_malformed_corpus_is_refused_naming_the_file(
        self, tmp_path: Path, files: dict[str, Any], reason: str
    ) -> None:
        directory = _write(tmp_path / "kb", files)
        with pytest.raises(Bm25CorpusError, match=reason) as excinfo:
            load_bm25_corpus(directory, Bm25DocumentsConfig())
        assert any(name in str(excinfo.value) for name in files)

    def test_extra_json_keys_are_ignored(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb", {"d.json": {**_json_doc("d", "D", "text"), "metadata": {"k": 1}}}
        )
        (document,) = load_bm25_corpus(directory, Bm25DocumentsConfig())
        assert document.content == "text"

    def test_an_empty_or_missing_corpus_refuses_the_trial(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SearchIndexBuildError, match="holds no document"):
            _index(_backend(), empty)
        with pytest.raises(SearchIndexBuildError, match="does not exist"):
            _index(_backend(), tmp_path / "missing")
        with pytest.raises(SearchIndexBuildError, match="declares no documents_path"):
            asyncio.run(_backend().build_index(None))

    def test_build_index_refuses_a_trial_less_context(self, corpus: Path) -> None:
        with pytest.raises(RuntimeError, match="trial-less"):
            _index(_backend(trial_id=None), corpus)

    def test_the_refusal_names_the_trial_and_the_file(self, tmp_path: Path) -> None:
        directory = _write(tmp_path / "kb", {"blank.md": ""})
        with pytest.raises(SearchIndexBuildError) as excinfo:
            _index(_backend(), directory)
        assert str(excinfo.value).startswith(f"Trial {TRIAL_ID}: search backend 'bm25'")
        assert "blank.md" in str(excinfo.value)


class TestAnUnscorableCorpus:
    """A corpus BM25 cannot score is refused through ``build_index``, never a crash."""

    def test_a_blank_indexed_field_refuses_the_trial_naming_the_file(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb",
            {"a.json": _json_doc("a", "Alpha", "first"), "b.json": _json_doc("b", "  ", "second")},
        )
        with pytest.raises(SearchIndexBuildError) as excinfo:
            _index(_backend({"documents": {"fields": ["title"]}}), directory)

        message = str(excinfo.value)
        assert message.startswith(f"Trial {TRIAL_ID}: search backend 'bm25' cannot index ")
        assert message.endswith("b.json: document 'b' has no text in its indexed fields ['title']")

    def test_a_corpus_whose_titles_are_all_blank_is_refused_under_fields_title(
        self, tmp_path: Path
    ) -> None:
        """The case that divided by zero: content validates, the indexed titles are empty."""
        directory = _write(
            tmp_path / "kb",
            {"a.json": _json_doc("a", "", "first"), "b.json": _json_doc("b", " ", "second")},
        )
        with pytest.raises(SearchIndexBuildError, match="a.json: document 'a' has no text"):
            _index(_backend({"documents": {"fields": ["title"]}}), directory)

    def test_the_title_field_indexes_when_every_title_is_present(self, corpus: Path) -> None:
        outcome = _search(_index(_backend({"documents": {"fields": ["title"]}}), corpus), "returns")
        assert outcome.hits[0].doc_id == "ret-1"
        assert outcome.hits[0].score > 0

    def test_a_corpus_that_tokenizes_to_no_term_is_refused(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-blank text a tokenizer drops entirely leaves BM25 nothing to average."""
        monkeypatch.setitem(TOKENIZERS, "no_terms", lambda text: [])
        with pytest.raises(SearchIndexBuildError) as excinfo:
            _index(_backend({"tokenizer": "no_terms"}), corpus)

        message = str(excinfo.value)
        assert message.startswith(f"Trial {TRIAL_ID}: search backend 'bm25' cannot index ")
        assert "none of the 4 documents' indexed text tokenizes to a term" in message

    def test_the_scorer_refuses_an_empty_vocabulary_and_scores_a_partly_empty_corpus(
        self,
    ) -> None:
        with pytest.raises(Bm25CorpusError, match="tokenizes to a term"):
            OkapiBm25([[], []])
        with pytest.raises(Bm25CorpusError, match="at least one document"):
            OkapiBm25([])
        scores = OkapiBm25([[], ["halden", "code"], ["other"]]).get_scores(["halden"])
        assert scores[1] > 0 and scores[0] == 0.0


# =============================================================================
# Tokenizer and indexed fields
# =============================================================================


class TestTokenizer:
    def test_whitespace_lower_is_lower_cased_whitespace_splitting(self) -> None:
        tokenize = TOKENIZERS["whitespace_lower"]
        assert tokenize("Refund  Window,\tthirty\nDAYS.") == [
            "refund",
            "window,",
            "thirty",
            "days.",
        ]
        assert tokenize("   ") == []

    def test_the_registry_holds_the_one_named_tokenizer(self) -> None:
        assert set(TOKENIZERS) == {"whitespace_lower"}

    def test_a_title_is_searchable_only_when_fields_index_it(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb",
            {
                "a.json": _json_doc("a", "Halden substation", "procedure text"),
                "b.json": _json_doc("b", "Other", "halden mentioned in the body"),
                "c.json": _json_doc("c", "Contacts", "who to call"),
                "d.json": _json_doc("d", "Windows", "maintenance windows"),
                "e.json": _json_doc("e", "Codes", "authorization codes"),
            },
        )
        content_only = _search(_index(_backend(), directory), "halden")
        assert [hit.doc_id for hit in content_only.hits] == ["b", "a", "c", "d", "e"]
        assert content_only.hits[0].score > 0
        assert content_only.hits[1].score == 0.0, "zero scores are kept"

        with_title = _backend({"documents": {"fields": ["title", "content"]}})
        both = _search(_index(with_title, directory), "halden")
        assert [hit.doc_id for hit in both.hits[:2]] == ["a", "b"]
        assert all(hit.score > 0 for hit in both.hits[:2])
        assert all(hit.score == 0 for hit in both.hits[2:])


# =============================================================================
# Ranking
# =============================================================================


def _seven_docs(tmp_path: Path) -> Path:
    """Seven short documents: ``apple`` in three, ``banana`` in two, the rest unique."""
    return _write(
        tmp_path / "kb",
        {
            "1.txt": "apple banana",
            "2.txt": "apple banana",
            "3.txt": "apple",
            "4.txt": "cherry",
            "5.txt": "durian",
            "6.txt": "elderberry",
            "7.txt": "fig",
        },
    )


class TestRanking:
    def test_ties_rank_in_corpus_order_and_zero_scores_are_kept(self, tmp_path: Path) -> None:
        outcome = _search(_index(_backend(), _seven_docs(tmp_path)), "banana apple")
        ids = [hit.doc_id for hit in outcome.hits]
        assert ids == ["1", "2", "3", "4", "5"], "five of seven: the default top_k"
        assert outcome.hits[0].score == outcome.hits[1].score > outcome.hits[2].score > 0
        assert outcome.hits[3].score == outcome.hits[4].score == 0.0

    def test_top_k_caps_the_hits_and_exceeds_the_corpus_harmlessly(self, tmp_path: Path) -> None:
        directory = _seven_docs(tmp_path)
        assert (
            len(_search(_index(_backend({"ranking": {"top_k": 2}}), directory), "apple").hits) == 2
        )
        assert (
            len(_search(_index(_backend({"ranking": {"top_k": 50}}), directory), "apple").hits) == 7
        )

    def test_min_score_drops_the_documents_below_it(self, tmp_path: Path) -> None:
        directory = _seven_docs(tmp_path)
        kept_zero = _search(
            _index(_backend({"ranking": {"min_score": 0.0, "top_k": 9}}), directory), "apple"
        )
        assert len(kept_zero.hits) == 7, "min_score 0.0 keeps zero scores"
        positive = _search(_index(_backend({"ranking": {"min_score": 1e-9}}), directory), "apple")
        assert [hit.doc_id for hit in positive.hits] == ["3", "1", "2"]
        nothing = _search(_index(_backend({"ranking": {"min_score": 100.0}}), directory), "apple")
        assert nothing.hits == ()

    def test_a_query_touching_nothing_returns_the_corpus_head(self, tmp_path: Path) -> None:
        outcome = _search(_index(_backend({"ranking": {"top_k": 2}}), _seven_docs(tmp_path)), "zzz")
        assert [(hit.doc_id, hit.score) for hit in outcome.hits] == [("1", 0.0), ("2", 0.0)]

    def test_hits_carry_the_whole_document_and_the_file_name(self, corpus: Path) -> None:
        outcome = _search(_index(_backend(), corpus), "refund")
        top = outcome.hits[0]
        assert top == SearchHit(
            doc_id="ret-1",
            source="a_returns.json",
            score=top.score,
            text="refund window thirty days",
        )
        assert isinstance(outcome.hits, tuple)

    def test_bm25_constants_change_the_scores(self, corpus: Path) -> None:
        default = _search(_index(_backend(), corpus), "refund warranty").hits[0].score
        tuned = _backend({"bm25": {"k1": 0.5, "b": 0.1, "epsilon": 0.9}})
        assert _search(_index(tuned, corpus), "refund warranty").hits[0].score != default


# =============================================================================
# The agent's arguments and schema
# =============================================================================


class TestAgentParameters:
    def test_query_alone_by_default(self) -> None:
        parameters = _backend().tool_parameters()
        assert parameters == {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find relevant documents",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def test_top_k_is_exposed_with_the_configured_default(self) -> None:
        backend = _backend({"agent_parameters": ["query", "top_k"], "ranking": {"top_k": 3}})
        top_k = backend.tool_parameters()["properties"]["top_k"]
        assert top_k == {
            "type": "integer",
            "description": "Number of results to return (default: 3)",
            "default": 3,
            "minimum": 1,
        }
        assert backend.tool_parameters()["required"] == ["query"]

    def test_each_call_gets_a_fresh_parameters_object(self) -> None:
        backend = _backend()
        first = backend.tool_parameters()
        first["properties"]["query"]["description"] = "mutated"
        assert backend.tool_parameters()["properties"]["query"]["description"] != "mutated"

    def test_an_exposed_top_k_overrides_the_configured_one(self, tmp_path: Path) -> None:
        backend = _backend({"agent_parameters": ["query", "top_k"], "ranking": {"top_k": 1}})
        index = _index(backend, _seven_docs(tmp_path))
        assert len(_search(index, "apple", {"top_k": 3}).hits) == 3
        assert len(_search(index, "apple", {"top_k": None}).hits) == 1
        assert len(_search(index, "apple").hits) == 1

    def test_an_unexposed_top_k_argument_is_ignored(self, tmp_path: Path) -> None:
        index = _index(_backend({"ranking": {"top_k": 1}}), _seven_docs(tmp_path))
        assert len(_search(index, "apple", {"top_k": 3}).hits) == 1

    @pytest.mark.parametrize("top_k", [0, -2, "3", 2.5, True])
    def test_an_exposed_top_k_outside_the_schema_raises(self, tmp_path: Path, top_k: Any) -> None:
        backend = _backend({"agent_parameters": ["query", "top_k"]})
        index = _index(backend, _seven_docs(tmp_path))
        with pytest.raises(ValueError, match="top_k must be a positive integer"):
            _search(index, "apple", {"top_k": top_k})

    def test_a_non_string_query_raises(self, tmp_path: Path) -> None:
        index = _index(_backend(), _seven_docs(tmp_path))
        with pytest.raises(TypeError, match="query must be a string"):
            asyncio.run(index.search(5, {"query": 5}, budget_s=1.0))  # type: ignore[arg-type]


# =============================================================================
# Empty queries
# =============================================================================


class TestEmptyQuery:
    @pytest.mark.parametrize("query", ["", "   ", "\n\t"])
    def test_no_results_renders_the_empty_text_and_no_hits(self, corpus: Path, query: str) -> None:
        outcome = _search(_index(_backend(), corpus), query)
        assert outcome.hits == ()
        assert json.loads(outcome.rendered) == {
            "message": "No relevant documents found.",
            "results": [],
            "query": query,
        }

    def test_error_renders_the_error_text_and_nothing_raises(self, corpus: Path) -> None:
        outcome = _search(_index(_backend({"empty_query": "error"}), corpus), "  ")
        assert outcome.hits == ()
        assert json.loads(outcome.rendered) == {"error": "Query is required", "results": []}

    def test_the_texts_are_configurable_in_both_renderers(self, corpus: Path) -> None:
        as_json = _backend(
            {"empty_query": "error", "render": {"kind": "json", "error_text": "Say something."}}
        )
        assert (
            json.loads(_search(_index(as_json, corpus), "").rendered)["error"] == "Say something."
        )
        as_text = _backend(
            {
                "empty_query": "error",
                "render": {"kind": "text", "error_text": "ERROR: empty query"},
            }
        )
        assert _search(_index(as_text, corpus), "").rendered == "ERROR: empty query"
        no_results = _backend({"render": {"kind": "text", "empty_text": "Nothing."}})
        assert _search(_index(no_results, corpus), "").rendered == "Nothing."

    def test_the_judge_finds_nothing_for_a_blank_query(self, corpus: Path) -> None:
        assert _index(_backend(), corpus).knowledge_search().search("  ") == []


# =============================================================================
# Rendering
# =============================================================================


class TestJsonRenderer:
    def test_the_agent_reads_results_total_and_query(self, corpus: Path) -> None:
        outcome = _search(_index(_backend({"ranking": {"top_k": 2}}), corpus), "refund")
        rendered = json.loads(outcome.rendered)
        assert list(rendered) == ["results", "total", "query"]
        assert rendered["total"] == 2 and rendered["query"] == "refund"
        first = rendered["results"][0]
        assert list(first) == ["doc_id", "title", "source", "score", "text"]
        assert first == {
            "doc_id": "ret-1",
            "title": "Returns",
            "source": "a_returns.json",
            "score": outcome.hits[0].score,
            "text": "refund window thirty days",
        }

    def test_no_hits_renders_a_message(self, tmp_path: Path) -> None:
        backend = _backend({"ranking": {"min_score": 1.0}})
        outcome = _search(_index(backend, _seven_docs(tmp_path)), "zzz")
        assert json.loads(outcome.rendered) == {
            "message": "No relevant documents found.",
            "results": [],
            "query": "zzz",
        }

    def test_non_ascii_text_is_not_escaped(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb", {"ru.json": _json_doc("ru", "Возврат", "окно возврата")}
        )
        outcome = _search(_index(_backend(), directory), "возврата")
        assert "окно возврата" in outcome.rendered


class TestTextRenderer:
    def test_the_default_shape_numbers_hits_and_formats_the_score(self, corpus: Path) -> None:
        backend = _backend({"render": {"kind": "text"}, "ranking": {"top_k": 2}})
        outcome = _search(_index(backend, corpus), "refund")
        first, second = outcome.hits
        assert outcome.rendered == (
            f"1. Returns\n   ID: ret-1\n   Score: {first.score:.4f}\n"
            f"   Content: refund window thirty days\n"
            "\n"
            f"2. Shipping\n   ID: ship-1\n   Score: {second.score:.4f}\n"
            f"   Content: orders ship in two days\n"
        )
        assert first.score > second.score == 0.0

    def test_every_template_field_and_the_separator_are_configurable(self, corpus: Path) -> None:
        backend = _backend(
            {
                "render": {
                    "kind": "text",
                    "item_template": "[{index}] {id} | {title} | {source} | {score} | {content:.6}",
                    "separator": " || ",
                    "score_format": ".1f",
                },
                "ranking": {"top_k": 2},
            }
        )
        outcome = _search(_index(backend, corpus), "refund")
        first, second = outcome.hits
        assert outcome.rendered == (
            f"[1] ret-1 | Returns | a_returns.json | {first.score:.1f} | refund"
            f" || [2] ship-1 | Shipping | b_shipping.json | {second.score:.1f} | orders"
        )

    def test_a_measured_timing_suffix_carries_integer_milliseconds(self, corpus: Path) -> None:
        backend = _backend(
            {"render": {"kind": "text", "timing_suffix": "measured"}, "ranking": {"top_k": 1}}
        )
        outcome = _search(_index(backend, corpus), "refund")
        body, suffix = outcome.rendered.split("\n\n[Timing: ")
        assert body.endswith("Content: refund window thirty days\n")
        match = re.fullmatch(r"retrieval=(\d+)ms, total=(\d+)ms\]", suffix)
        assert match, suffix
        retrieval, total = (int(group) for group in match.groups())
        assert 0 <= retrieval <= total < 10_000

    def test_the_timing_template_can_carry_a_reranking_segment(self, corpus: Path) -> None:
        backend = _backend(
            {
                "render": {
                    "kind": "text",
                    "timing_suffix": "measured",
                    "timing_template": (
                        "\n\n[Timing: retrieval={retrieval_ms}ms, reranking={reranking_ms}ms, "
                        "total={total_ms}ms]"
                    ),
                }
            }
        )
        outcome = _search(_index(backend, corpus), "refund")
        assert re.search(
            r"\[Timing: retrieval=\d+ms, reranking=0ms, total=\d+ms\]$", outcome.rendered
        )

    def test_the_suffix_follows_no_results_but_never_the_error_text(self, tmp_path: Path) -> None:
        config = {"render": {"kind": "text", "timing_suffix": "measured"}}
        no_results = _search(_index(_backend(config), _seven_docs(tmp_path)), "")
        assert no_results.rendered.startswith("No results found.\n\n[Timing: retrieval=0ms")
        filtered = _backend({**config, "ranking": {"min_score": 5.0}})
        assert _search(_index(filtered, _seven_docs(tmp_path)), "apple").rendered.startswith(
            "No results found.\n\n[Timing: "
        )
        error = _backend({**config, "empty_query": "error"})
        assert _search(_index(error, _seven_docs(tmp_path)), "").rendered == (
            "Error: the query must not be empty."
        )

    def test_braces_in_a_document_are_rendered_verbatim(self, tmp_path: Path) -> None:
        directory = _write(
            tmp_path / "kb", {"c.json": _json_doc("c", "Code {x}", "use {query} here")}
        )
        outcome = _search(_index(_backend({"render": {"kind": "text"}}), directory), "use")
        assert "1. Code {x}\n   ID: c\n" in outcome.rendered
        assert "Content: use {query} here" in outcome.rendered


# =============================================================================
# The judge's search and the cache
# =============================================================================


class TestKnowledgeSearch:
    def test_the_judge_reads_whole_documents_from_the_agents_index(self, corpus: Path) -> None:
        index = _index(_backend({"ranking": {"top_k": 1}}), corpus)
        agent = _search(index, "warranty shipping")
        judge = index.knowledge_search()
        assert isinstance(judge, KnowledgeSearch) and isinstance(judge, Bm25KnowledgeSearch)
        assert judge.index is index
        hits = judge.search("warranty shipping", top_k=3, alpha=0.9)
        assert [hit.doc_id for hit in hits][:1] == [agent.hits[0].doc_id]
        assert len(hits) == 3, "the judge's top_k is its own, not the agent's"
        assert all(
            hit.text == next(d for d in index.corpus.documents if d.id == hit.doc_id).content
            for hit in hits
        )

    def test_the_judge_honours_min_score_and_refuses_a_bad_top_k(self, tmp_path: Path) -> None:
        index = _index(_backend({"ranking": {"min_score": 1e-9}}), _seven_docs(tmp_path))
        assert [hit.doc_id for hit in index.knowledge_search().search("cherry", top_k=4)] == ["4"]
        with pytest.raises(ValueError, match="top_k"):
            index.knowledge_search().search("cherry", top_k=0)

    def test_the_index_satisfies_the_protocol(self, corpus: Path) -> None:
        assert isinstance(_index(_backend(), corpus), SearchIndex)


class TestInProcessCache:
    def test_the_same_corpus_and_config_share_one_built_index(
        self, corpus: Path, tmp_path: Path
    ) -> None:
        first = _index(_backend(), corpus)
        second = _index(_backend({"ranking": {"top_k": 5}}), corpus)  # the same effective config
        assert second.corpus is first.corpus

        copied = tmp_path / "another_extraction"
        copied.mkdir()
        for path in corpus.iterdir():
            (copied / path.name).write_bytes(path.read_bytes())
        assert _index(_backend(), copied).corpus is first.corpus, "keyed by content, not path"

    def test_a_different_config_or_corpus_builds_anew(self, corpus: Path) -> None:
        first = _index(_backend(), corpus)
        assert _index(_backend({"bm25": {"k1": 1.2}}), corpus).corpus is not first.corpus
        assert _index(_backend({"ranking": {"top_k": 7}}), corpus).corpus is not first.corpus
        (corpus / "e_extra.md").write_text("a new document")
        assert _index(_backend(), corpus).corpus is not first.corpus

    def test_the_cache_key_is_the_corpus_digest_and_the_config_digest(self, corpus: Path) -> None:
        index = _index(_backend(), corpus)
        corpus_digest, config_digest = index.corpus.cache_key
        assert len(corpus_digest) == len(config_digest) == 64
        assert config_digest == Bm25BackendConfig().fingerprint()

    def test_a_build_reads_each_corpus_file_once(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fingerprint and the documents come from one read of each file's bytes."""
        reads: list[str] = []
        real_read_bytes = Path.read_bytes

        def counting_read_bytes(self: Path) -> bytes:
            reads.append(self.name)
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        monkeypatch.setattr(Path, "read_text", lambda self, **kw: pytest.fail("read_text used"))
        files = sorted(path.name for path in corpus.iterdir())
        _index(_backend(), corpus)
        assert sorted(reads) == files, "a cold build"
        reads.clear()
        _index(_backend(), corpus)
        assert sorted(reads) == files, "a cache hit still reads the bytes it is keyed by, once"

    def test_clearing_the_cache_rebuilds(self, corpus: Path) -> None:
        first = _index(_backend(), corpus)
        clear_index_cache()
        assert _index(_backend(), corpus).corpus is not first.corpus
