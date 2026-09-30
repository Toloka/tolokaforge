"""Search-backend Protocols and the context a backend factory receives (ADR-0052).

A task selects its retrieval the way it selects every other swappable part of a
trial: by name, through an entry-point group. The name is
``initial_state.rag.backend``; the native adapter carries it on the wire as
``TaskDescription.search.plane``, and the runner resolves it through the
``tolokaforge.search_backends`` group with
:func:`~tolokaforge.core.plugin_registry.load_search_backend`. The engine's own
rag-service backend registers there as ``rag_service`` and resolves like any
third-party one.

Two Protocols, one context, one factory alias:

* :class:`SearchBackend` — what a task selects. It declares its ``name``, the
  stack service it needs (``stack_service``; the orchestrator's stack rule reads
  it), the JSON-schema ``parameters`` object of the agent's tool
  (:meth:`~SearchBackend.tool_parameters`), and builds one trial's
  :class:`SearchIndex` from the corpus.
* :class:`SearchIndex` — one trial's index. :meth:`~SearchIndex.search` answers
  the agent's tool call with a :class:`SearchOutcome`: the hits, and the text the
  agent reads. The backend owns that rendering, because the agent-visible text is
  part of what a backend reproduces. :meth:`~SearchIndex.knowledge_search` hands
  the judge a :class:`~tolokaforge.core.grading.kb_search.KnowledgeSearch` over
  the same index, or ``None`` when the backend gives the judge nothing.
* :class:`SearchBackendContext` — what a factory is built from.
* :data:`SearchBackendFactory` — ``Callable[[SearchBackendContext], SearchBackend]``,
  the shape every ``tolokaforge.search_backends`` entry point resolves to.

Both calls that do trial work are coroutines. The runner awaits
:meth:`~SearchBackend.build_index` on its own event loop at ``RegisterTrial`` and
the agent's tool call awaits :meth:`~SearchIndex.search` on the same loop, so a
backend over a network service (rag-service's HTTP API) needs no bridge of its
own, and an in-process backend simply never awaits.

**One context, two callers.** The runner builds a context per trial, at
``RegisterTrial``. The orchestrator side — the native adapter building the
agent's schema, the stack rule choosing the stack — builds a *trial-less* one:
``trial_id`` is ``None`` and ``stack_service_clients`` is empty. It needs only
what a backend declares (``tool_parameters()``, ``stack_service``), and those may
depend on ``backend_config`` — which is why they are read off a constructed
backend rather than declared on a class. A factory must therefore be cheap and
free of side effects; every piece of trial work belongs in ``build_index``, which
a backend refuses on a trial-less context.

**How a backend reaches its stack service.** A backend that declares a
``stack_service`` needs the runner's handle on that service — rag-service's
indexing and search go through the runner's one long-lived client, bound to the
runner's event loop and shared with the judge's search, so a backend building a
client of its own would change connection handling and could let the judge and
the agent reach different endpoints. The context carries the runner's handles in
``stack_service_clients``, keyed by the ``stack_service`` name a backend
declares; the backend reads the entry its own ``stack_service`` names. The
values are typed ``object`` because a handle's type belongs to its service, not
to this seam: this module names no service, and a backend narrows the value it
reads to the client type it was written against. A mapping rather than a single
slot, because the runner builds the context before the factory has said which
service it declares.

This module imports the standard library and the judge's search contract only,
so the runner subset ships it without dragging any backend's dependencies in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit

__all__ = [
    "SearchBackend",
    "SearchBackendContext",
    "SearchBackendFactory",
    "SearchIndex",
    "SearchIndexBuildError",
    "SearchOutcome",
]


@dataclass(frozen=True)
class SearchOutcome:
    """What one search answered: the hits, and the text the agent receives.

    ``hits`` is what a reader of the retrieval itself consumes — the judge's
    search, the remote grader's ``KBSearch`` and offline replay all read
    :class:`~tolokaforge.core.grading.kb_search.SearchHit`. ``rendered`` is the
    tool result the agent reads, exactly as the backend formats it.
    """

    hits: tuple[SearchHit, ...]
    rendered: str


@runtime_checkable
class SearchIndex(Protocol):
    """One trial's index over its corpus, built by :meth:`SearchBackend.build_index`.

    A failed search raises; it is never rendered as empty results, because an
    agent reading "no documents found" from a dead index is graded for the
    outage.
    """

    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome:
        """Answer one call of the agent's search tool.

        ``query`` is the call's ``query`` argument as the agent sent it;
        ``arguments`` is the whole call, for the parameters the backend's
        :meth:`SearchBackend.tool_parameters` exposes beside it. ``budget_s`` is
        the tool's declared per-call budget: a backend whose search is a bounded
        call — an HTTP request — bounds it by this, so the runner's backstop,
        which sits above the budget, never fires first.
        """
        ...

    def knowledge_search(self) -> KnowledgeSearch | None:
        """The judge's read over this same index, or ``None`` to give it nothing."""
        ...


@runtime_checkable
class SearchBackend(Protocol):
    """A retrieval implementation a task selects by name (``search.plane``).

    ``name`` is the name the backend is registered under. ``stack_service`` names
    the stack service the backend needs, or is ``None`` for a backend that runs
    in the runner process alone: the orchestrator starts ``full_stack`` for a task
    whose backend declares ``"rag_service"``, and the runner hands the backend its
    handle on that service through
    :attr:`SearchBackendContext.stack_service_clients`.
    """

    name: str
    stack_service: str | None

    def tool_parameters(self) -> Mapping[str, Any] | None:
        """The JSON-schema ``parameters`` object of the agent's search tool.

        The whole object the agent's tool schema carries — ``type``,
        ``properties``, ``required`` — and it must declare a ``query`` property:
        the runner hands that argument to :meth:`SearchIndex.search` by name.
        ``None`` when the backend gives the agent no tool of its own.
        """
        ...

    async def build_index(self, corpus_dir: Path | None) -> SearchIndex:
        """Build this trial's index from the corpus the task shipped.

        ``corpus_dir`` is the task's ``search.documents_path``, resolved by the
        runner against the trial's extracted artifacts; ``None`` when the task
        declares none. Raises :class:`SearchIndexBuildError` with the refusal
        ``RegisterTrial`` returns when the index cannot be built — a corpus that
        indexes empty included, since that is a bundling bug and not an agent
        failure.
        """
        ...


class SearchIndexBuildError(Exception):
    """The backend cannot build this trial's index.

    ``RegisterTrial`` returns the message verbatim as its refusal, so a backend
    words it for the operator reading why the trial never started.
    """


@dataclass(frozen=True)
class SearchBackendContext:
    """The inputs a search-backend factory receives.

    ``backend_config`` is the task's ``initial_state.rag.backend_config``, passed
    through verbatim: the engine never reads its keys, and a backend validates
    the mapping into a model of its own. ``tool_name`` and ``tool_description``
    are the agent's search tool as the task declares it (``initial_state.rag.tool``);
    ``tool_description`` is ``None`` at ``RegisterTrial`` when no actor's tool
    set carries the declared tool.

    ``trial_id``, ``domain_name`` and ``stack_service_clients`` are what the
    runner knows at ``RegisterTrial``: the trial, the knowledge base's
    ``search.domain_name``, and the runner's handle on each stack service it
    reaches, keyed by the ``stack_service`` name a backend declares. A context
    built orchestrator-side to read what a backend declares leaves ``trial_id``
    ``None`` and ``stack_service_clients`` empty; see the module docstring.
    """

    backend_config: Mapping[str, Any]
    tool_name: str
    tool_description: str | None
    logger: logging.Logger
    trial_id: str | None = None
    domain_name: str | None = None
    stack_service_clients: Mapping[str, object] = field(default_factory=dict)


SearchBackendFactory = Callable[[SearchBackendContext], SearchBackend]
