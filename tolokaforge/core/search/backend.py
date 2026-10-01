"""Search-backend Protocols and the context a backend factory receives (ADR-0053).

A task names its retrieval in ``initial_state.rag.backend``; the wire carries the
name as ``search.plane`` and the runner resolves it through the
``tolokaforge.search_backends`` group
(:func:`~tolokaforge.core.plugin_registry.load_search_backend`). The engine's
rag-service registers there as ``rag_service``, like any third-party backend.

* :class:`SearchBackend` — what a task selects: its ``name``, the
  ``stack_service`` it needs, the agent tool's ``parameters``, and
  ``build_index``.
* :class:`SearchIndex` — one trial's index: ``search`` answers the agent's call
  with a :class:`SearchOutcome` the backend renders itself; ``knowledge_search``
  gives the judge a read over the same index, or ``None``.
* :class:`SearchBackendContext` and :data:`SearchBackendFactory` — what an entry
  point is built from, and its shape.

``build_index`` and ``search`` are coroutines the runner awaits on its own event
loop. A factory is also built orchestrator-side from a *trial-less* context
(``trial_id`` ``None``, no stack-service handles) to read ``tool_parameters()`` and
``stack_service``, which may depend on ``backend_config``; so a factory does no
trial work, and ``build_index`` refuses that context.

A backend that declares a ``stack_service`` reads the runner's handle on it from
``stack_services``, the declared, versioned surface of
:mod:`tolokaforge.core.search.stack_services` — the runner's one client per service,
shared with the judge's search. This module imports only the standard library, that
surface and the judge's search contract, so the runner subset ships it light.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit
from tolokaforge.core.search.stack_services import RAG_SERVICE_STACK_SERVICE, StackServices

__all__ = [
    "RAG_SERVICE_STACK_SERVICE",
    "SearchBackend",
    "SearchBackendContext",
    "SearchBackendFactory",
    "SearchIndex",
    "SearchIndexBuildError",
    "SearchOutcome",
    "StackServices",
]


@dataclass(frozen=True)
class SearchOutcome:
    """What one search answered: the hits, and the text the agent receives.

    ``rendered`` is the tool result the agent reads, exactly as the backend
    formats it. ``hits`` are the same answer in the backend-neutral
    :class:`~tolokaforge.core.grading.kb_search.SearchHit` shape the judge's
    search, the remote grader's ``KBSearch`` and offline replay read. No engine
    code reads an outcome's ``hits``; a backend fills them faithfully all the
    same, as the record of what the agent retrieved.
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
    the stack service the backend needs — one of
    :data:`~tolokaforge.core.search.stack_services.DECLARED_STACK_SERVICES` — or is
    ``None`` for a backend that runs in the runner process alone. The orchestrator
    starts ``full_stack`` for a task whose backend declares ``"rag_service"``, and
    the runner builds the trial's index only when it reaches the declared service,
    whose handle the backend reads from :attr:`SearchBackendContext.stack_services`.
    A name outside the declared ones is refused at load and at ``RegisterTrial``.
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
        declares none. The native adapter declares a search block only for a task
        that declares a corpus, so today it is ``None`` only for an external
        adapter's task; a backend that needs no corpus is a follow-up. Raises
        :class:`SearchIndexBuildError` with the refusal ``RegisterTrial`` returns
        when the index cannot be built — a corpus that indexes empty included,
        since that is a bundling bug and not an agent failure.
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
    through verbatim as a read-only deep copy: the engine never reads its keys, a
    backend validates the mapping into a model of its own, and nothing a backend
    does changes the config the trial is graded and bundled with. ``tool_name`` and ``tool_description``
    are the agent's search tool as the task declares it (``initial_state.rag.tool``);
    ``tool_description`` is ``None`` at ``RegisterTrial`` when no actor's tool
    set carries the declared tool.

    ``trial_id``, ``domain_name`` and ``stack_services`` are what the runner knows
    at ``RegisterTrial``: the trial, the knowledge base's ``search.domain_name``,
    and the runner's handle on each declared stack service it reaches
    (:class:`~tolokaforge.core.search.stack_services.StackServices`; a backend
    reads one with ``stack_services.get(RAG_SERVICE)``). A context built
    orchestrator-side to read what a backend declares leaves ``trial_id`` ``None``
    and ``stack_services`` empty; see the module docstring.
    """

    backend_config: Mapping[str, Any]
    tool_name: str
    tool_description: str | None
    logger: logging.Logger
    trial_id: str | None = None
    domain_name: str | None = None
    stack_services: StackServices = field(default_factory=StackServices)

    def __post_init__(self) -> None:
        # The task's config is graded and bundled as the task declared it, so a
        # backend reads a read-only deep copy and cannot change what it was given.
        # The handles are the runner's own: shared, never copied.
        frozen = MappingProxyType(copy.deepcopy(dict(self.backend_config)))
        object.__setattr__(self, "backend_config", frozen)
        if not isinstance(self.stack_services, StackServices):
            raise TypeError(
                f"stack_services is {type(self.stack_services).__name__}; a backend reads "
                "the runner's handles from a StackServices, the declared stack-service "
                "surface (tolokaforge.core.search.stack_services)"
            )


SearchBackendFactory = Callable[[SearchBackendContext], SearchBackend]
