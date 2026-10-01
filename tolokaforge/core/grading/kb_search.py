"""Backend-neutral knowledge-base search contract for the rubric judge.

The judge must search the **same per-trial index the agent searched**, via a
tolokaforge-owned interface — never by re-deriving a backend URL or hitting a
global endpoint. This module defines that contract (:class:`KnowledgeSearch`)
plus the rag-service implementation that binds a trial's RAG client + trial id
to the per-trial ``/trials/{trial_id}/search`` endpoint.

Layering (AGENTS.md #6 / #7 — clean boundaries, interface-first):

* ``KnowledgeSearch`` is a read-only :class:`typing.Protocol`. It is a *behaviour
  contract* the judge depends on; per the type-system guidance it is a Protocol,
  not Pydantic. The judge (:mod:`tolokaforge.core.grading.judge`) imports only
  this contract — never ``runner`` internals or mcp_core.
* A search backend's per-trial index hands the judge its implementation
  (:meth:`~tolokaforge.core.search.backend.SearchIndex.knowledge_search`, ADR-0053);
  the runner binds it when an agent tool searched that same index. Two more
  implementations serve other readers: the remote grader's gRPC ``KBSearch``
  transport (``substrate_live``) and offline replay's recorded answers (``replay``).
* :class:`RagServiceKnowledgeSearch` is the ``rag_service`` backend's
  implementation. It lives here (its only dependency is the runner's declared
  rag-service handle, :class:`~tolokaforge.core.search.stack_services.RagServiceHandle`,
  which pulls in neither mcp_core nor the runner).
  TypeSense has none: a TypeSense task's judge reuses the agent's ``search_policy``
  tool through a read-only passthrough instead (``runner/service.py``).

Reconciliation with ``tolokaforge/core/search/typesense.py`` (deliberate, see
the plan's Decisions table): that module's ``SearchResult`` / ``SearchResponse``
are **TypeSense-shaped** — ``content: dict[str, Any]``, ``highlights: dict[str,
list[str]]``, and its ``search`` ABC takes ``collection_name`` + ``query_by`` +
``filter_by``. Those are backend-specific (TypeSense full-text field selectors
and a raw document dict), so reusing them as the judge's contract would leak
TypeSense semantics into a backend-neutral grading interface and force the
rag-service impl to invent empty ``content`` / ``highlights``. We therefore
define a minimal :class:`SearchHit` (doc id, source, score, text snippet) — the
common denominator both backends can populate honestly — and keep
``typesense.py`` as the (separate) TypeSense backend ABC it already is. No
silent duplicate: ``SearchHit`` is the judge contract; ``SearchResult`` is the
TypeSense wire shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from tolokaforge.core.search.stack_services import RagServiceHandle


@dataclass(frozen=True)
class SearchHit:
    """One knowledge-base hit, backend-neutral.

    The minimal common denominator across RAG backends — every backend can
    populate these honestly. ``score`` is "higher is better"; ``text`` is the
    snippet/document text the judge reads.
    """

    doc_id: str
    source: str
    score: float
    text: str


@runtime_checkable
class KnowledgeSearch(Protocol):
    """Read-only KB search the judge is given iff the agent had one this trial.

    Implementations are **per-trial** and point at the SAME index/collection the
    agent's KB tool used — that faithfulness is the whole point of the contract.
    A search backend's per-trial index supplies one through
    :meth:`~tolokaforge.core.search.backend.SearchIndex.knowledge_search`;
    tolokaforge ships :class:`RagServiceKnowledgeSearch` for ``rag_service``.

    Implementations MUST fail loud on transport/connection errors (raise), never
    degrade a failed search into empty results (AGENTS.md #1).
    """

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        """Return up to ``top_k`` hits for ``query``.

        ``alpha`` is a hybrid keyword/semantic weight; a backend without one
        ignores it. rag-service honours it.
        """
        ...


class RagServiceKnowledgeSearch:
    """rag-service impl of :class:`KnowledgeSearch`, bound to one trial.

    Queries the **per-trial** ``/trials/{trial_id}/search`` endpoint — the SAME
    index the agent's search tool (the ``rag_service`` backend, through
    ``RagServiceHandle.search``) used.

    Async/sync boundary: ``RagServiceHandle.search`` is async, but the judge loop
    runs synchronously in a worker thread (``run_in_executor``). Rather than bridge
    an async client across threads, this impl issues a **direct sync
    ``httpx.post``** to the per-trial endpoint, with the service's request schema.
    It reuses ``base_url`` + ``timeout`` from the trial's rag-service handle, so it
    queries exactly the service the agent did.

    Fail-loud: any HTTP/transport error propagates as :class:`httpx.HTTPError`;
    the search is never silently turned into empty results.
    """

    def __init__(self, rag_client: RagServiceHandle, trial_id: str):
        self._base_url = rag_client.base_url.rstrip("/")
        self._timeout = rag_client.timeout
        self._trial_id = trial_id

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        response = httpx.post(
            f"{self._base_url}/trials/{self._trial_id}/search",
            json={"query": query, "top_k": top_k, "alpha": alpha},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        # Per-trial endpoint returns {"results": [...]}; the legacy global
        # endpoint returned a bare list. Accept both so the impl is robust to the
        # service's response shape, without ever hitting the global path.
        rows = data["results"] if isinstance(data, dict) else data
        return [
            SearchHit(
                doc_id=row["doc_id"],
                source=row["source"],
                score=float(row["score"]),
                text=row["text"],
            )
            for row in rows
        ]


__all__ = [
    "KnowledgeSearch",
    "RagServiceKnowledgeSearch",
    "SearchHit",
]
