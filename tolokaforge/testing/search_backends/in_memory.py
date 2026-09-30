"""The in-memory reference :class:`~tolokaforge.core.search.backend.SearchBackend`.

:class:`InMemorySearchBackend` is the shortest backend that satisfies the seam. It
needs no stack service and holds nothing beyond one trial's documents, so an
implementer can read the whole contract in one screen and copy it:

* a factory that builds from a trial-less context, because the adapter and the
  stack rule build one orchestrator-side to read ``tool_parameters()`` and
  ``stack_service``;
* ``build_index`` doing the trial's work, refusing a trial-less context and a
  corpus with no documents;
* ``search`` returning a :class:`~tolokaforge.core.search.backend.SearchOutcome`
  whose ``rendered`` text the backend formats itself, and raising when it fails;
* ``knowledge_search`` over the same documents the agent searched.

:meth:`InMemorySearchIndex.fail_searches_with` stands in for the service an index
reaches going away, so the suite can check a failure is raised, not rendered.

It records what it was asked in :class:`InMemorySearchCallLog`, which is what lets
an end-to-end test show the agent's call and the judge's search reaching one index.

It is also the conformance suite's own control. Every switch in
:class:`SearchBackendDefects` breaks exactly one obligation without crashing, which
is what lets ``tests/canonical/test_search_backend_contract.py`` show each
conformance assertion failing on the backend that violates it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.search.backend import (
    SearchBackendContext,
    SearchIndexBuildError,
    SearchOutcome,
)

__all__ = [
    "IN_MEMORY_SEARCH_BACKEND_NAME",
    "InMemoryKnowledgeSearch",
    "InMemorySearchBackend",
    "InMemorySearchCallLog",
    "InMemorySearchIndex",
    "SearchBackendDefects",
    "in_memory_search_backend_factory",
]

IN_MEMORY_SEARCH_BACKEND_NAME = "in_memory"

_AGENT_TOP_K = 3
_NO_MATCH = "No document matched."


@dataclass(frozen=True)
class SearchBackendDefects:
    """One switch per obligation, each defaulting to "honoured"."""

    needs_a_trial_to_build: bool = False
    """Refuse a trial-less context: the adapter could no longer read the tool's schema."""

    parameters_without_query: bool = False
    """Declare a ``parameters`` object with no ``query``: the runner hands that argument over."""

    hits_as_a_list: bool = False
    """Return the hits as a list; ``SearchOutcome.hits`` is a tuple readers may hash."""

    rendered_as_a_mapping: bool = False
    """Hand back the rendering as a mapping; the agent's tool result is text."""

    judge_reads_another_index: bool = False
    """Give the judge a search over no documents: it reads a different index than the agent."""

    indexes_an_empty_corpus: bool = False
    """Build an index over a corpus with no documents instead of refusing the trial."""

    builds_without_a_trial: bool = False
    """Build an index from a trial-less context: trial work done for no trial."""

    renders_failed_searches_as_empty: bool = False
    """Answer a failed search with "no document matched": the agent is graded for an outage."""


@dataclass
class InMemorySearchCallLog:
    """What the backend was asked, for assertions across the runner and the judge."""

    builds: list[Path | None] = field(default_factory=list)
    searches: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    knowledge_searches: list[tuple[str, int]] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return {token for token in text.lower().split() if token}


class InMemorySearchIndex:
    """One trial's documents, ranked by how many query words each contains."""

    def __init__(
        self,
        documents: list[tuple[str, str, str]],
        call_log: InMemorySearchCallLog,
        defects: SearchBackendDefects,
    ) -> None:
        self.documents = documents
        self._call_log = call_log
        self._defects = defects
        self._failure: Exception | None = None

    def fail_searches_with(self, error: Exception) -> None:
        """Make every later search — the agent's and the judge's — raise ``error``."""
        self._failure = error

    def rank(self, query: str, top_k: int) -> tuple[SearchHit, ...]:
        """Documents sharing a word with ``query``, most shared first, ties by id."""
        if self._failure is not None:
            raise self._failure
        wanted = _tokens(query)
        scored = [
            (len(wanted & _tokens(text)), doc_id, source, text)
            for doc_id, source, text in self.documents
        ]
        ranked = sorted((row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]))
        return tuple(
            SearchHit(doc_id=doc_id, source=source, score=float(score), text=text)
            for score, doc_id, source, text in ranked[:top_k]
        )

    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome:
        self._call_log.searches.append((query, dict(arguments)))
        try:
            hits = self.rank(query, _AGENT_TOP_K)
        except Exception:
            if not self._defects.renders_failed_searches_as_empty:
                raise
            hits = ()
        rendered = "\n".join(f"[{hit.doc_id}] {hit.text}" for hit in hits) or _NO_MATCH
        if self._defects.rendered_as_a_mapping:
            return SearchOutcome(hits=hits, rendered={"text": rendered})  # type: ignore[arg-type]
        if self._defects.hits_as_a_list:
            return SearchOutcome(hits=list(hits), rendered=rendered)  # type: ignore[arg-type]
        return SearchOutcome(hits=hits, rendered=rendered)

    def knowledge_search(self) -> InMemoryKnowledgeSearch:
        if self._defects.judge_reads_another_index:
            return InMemoryKnowledgeSearch(
                InMemorySearchIndex([], self._call_log, self._defects), self._call_log
            )
        return InMemoryKnowledgeSearch(self, self._call_log)


class InMemoryKnowledgeSearch:
    """The judge's :class:`~tolokaforge.core.grading.kb_search.KnowledgeSearch` over an index."""

    def __init__(self, index: InMemorySearchIndex, call_log: InMemorySearchCallLog) -> None:
        self.index = index
        self._call_log = call_log

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        self._call_log.knowledge_searches.append((query, top_k))
        return list(self.index.rank(query, top_k))


class InMemorySearchBackend:
    """A backend over the corpus's ``.md`` / ``.txt`` files, in the runner process."""

    name = IN_MEMORY_SEARCH_BACKEND_NAME
    stack_service: str | None = None

    def __init__(
        self, context: SearchBackendContext, defects: SearchBackendDefects | None = None
    ) -> None:
        self._defects = defects or SearchBackendDefects()
        if self._defects.needs_a_trial_to_build and context.trial_id is None:
            raise ValueError("InMemorySearchBackend refuses a trial-less context (defect switch)")
        self.context = context
        self.backend_config = dict(context.backend_config)
        self.call_log = InMemorySearchCallLog()
        self.indexes: list[InMemorySearchIndex] = []

    def tool_parameters(self) -> Mapping[str, Any]:
        name = "text" if self._defects.parameters_without_query else "query"
        return {
            "type": "object",
            "properties": {name: {"type": "string", "description": "Words to look up"}},
            "required": [name],
            "additionalProperties": False,
        }

    async def build_index(self, corpus_dir: Path | None) -> InMemorySearchIndex:
        if self.context.trial_id is None and not self._defects.builds_without_a_trial:
            raise RuntimeError("InMemorySearchBackend builds an index only for a trial")
        self.call_log.builds.append(corpus_dir)
        if corpus_dir is None:
            raise SearchIndexBuildError(
                f"Trial {self.context.trial_id}: search backend {self.name!r} needs a corpus"
            )
        documents = [
            (path.stem, path.name, path.read_text(encoding="utf-8"))
            for path in sorted(corpus_dir.glob("*"))
            if path.suffix in {".md", ".txt"} and path.is_file()
        ]
        if not documents and not self._defects.indexes_an_empty_corpus:
            raise SearchIndexBuildError(
                f"Trial {self.context.trial_id}: corpus {corpus_dir} holds no documents"
            )
        index = InMemorySearchIndex(documents, self.call_log, self._defects)
        self.indexes.append(index)
        return index


def in_memory_search_backend_factory(context: SearchBackendContext) -> InMemorySearchBackend:
    """A :data:`~tolokaforge.core.search.backend.SearchBackendFactory` over the reference."""
    return InMemorySearchBackend(context)
