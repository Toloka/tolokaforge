"""The ``TrialObserver`` seam (ADR-0046).

The conductor opens and closes a trial, the tool-calling loop reports each generation and tool
call with its content, the orchestrator closes the run. Every implementation is called through
:func:`safely`, so an observer can never fail or slow a trial by raising. Parents are explicit
(the :class:`TrialIdentity` travels with every call); nothing here uses ambient context, because
trials run in parallel workers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.observability import ids

if TYPE_CHECKING:
    from tolokaforge.core.llm.client import GenerationResult
    from tolokaforge.core.models import Message, ToolCall, Trajectory
    from tolokaforge.tools.registry import ToolResult

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRef:
    """A model as the run config names it: the ``(provider, name)`` pair."""

    provider: str | None
    name: str


@dataclass(frozen=True)
class TrialIdentity:
    """Everything the id contract needs; ``trace_id`` is derived, never stored elsewhere."""

    run_id: str
    task_id: str
    trial_index: int
    attempt_id: int
    run_tag: str = ids.DEFAULT_RUN_TAG

    @property
    def trace_id(self) -> str:
        return ids.trace_id(
            run_tag=self.run_tag,
            run_id=self.run_id,
            task_id=self.task_id,
            trial_index=self.trial_index,
            attempt=self.attempt_id,
        )

    def observation_id(self, kind: str, *key: object) -> str:
        return ids.observation_id(self.trace_id, kind, *key)

    @property
    def root_id(self) -> str:
        """The trial's root span (contract v2: kind ``root``, key ``-``)."""
        return ids.observation_id(self.trace_id, "root", ids.ROOT_KEY)


@dataclass(frozen=True)
class ExportReceipt:
    """What left the process: the counts a run summary reports (ADR-0046, delivery contract)."""

    spans_queued: int = 0
    spans_exported: int = 0
    spans_dropped: int = 0
    export_failures: int = 0
    flushed: bool = True
    exporter: str = "none"
    # the post-trial attachment step (ADR-0046 amendment): files registered on their trace,
    # bytes uploaded by this run, registrations the receiver answered from bytes it already
    # held, files kept back by the data-safety scan, files that failed, manifests written
    attachments_registered: int = 0
    attachments_uploaded: int = 0
    attachments_deduplicated: int = 0
    attachments_skipped: int = 0
    attachments_failed: int = 0
    manifests_sent: int = 0
    manifests_failed: int = 0
    # the receiver-side project the credentials had to open, and the outcome of the check
    # (verified | unverified | none) run before the first export (destinations amendment)
    expect_project: str | None = None
    project_verified: str = "none"
    # the gradings amendment: per trial, the run's grading with its judge transcript and scores
    # and the simulated user turns leave from the persisted bundle through the ingestion API
    gradings_sent: int = 0
    gradings_failed: int = 0
    scores_sent: int = 0
    user_generations_sent: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "spans_queued": self.spans_queued,
            "spans_exported": self.spans_exported,
            "spans_dropped": self.spans_dropped,
            "export_failures": self.export_failures,
            "flushed": self.flushed,
            "exporter": self.exporter,
            "attachments_registered": self.attachments_registered,
            "attachments_uploaded": self.attachments_uploaded,
            "attachments_deduplicated": self.attachments_deduplicated,
            "attachments_skipped": self.attachments_skipped,
            "attachments_failed": self.attachments_failed,
            "manifests_sent": self.manifests_sent,
            "manifests_failed": self.manifests_failed,
            "expect_project": self.expect_project,
            "project_verified": self.project_verified,
            "gradings_sent": self.gradings_sent,
            "gradings_failed": self.gradings_failed,
            "scores_sent": self.scores_sent,
            "user_generations_sent": self.user_generations_sent,
        }


@runtime_checkable
class TrialObserver(Protocol):
    """Receives a trial's life: start, every generation and tool call, the graded end, run end."""

    def trial_started(
        self, identity: TrialIdentity, *, models: Mapping[str, ModelRef], started_at: datetime
    ) -> None: ...

    def generation(
        self,
        identity: TrialIdentity,
        *,
        role: str,
        index: int,
        turn: int,
        request: Sequence[Message],
        result: GenerationResult,
        started_at: datetime,
        ended_at: datetime,
    ) -> None: ...

    def tool_call(
        self,
        identity: TrialIdentity,
        *,
        role: str,
        index: int,
        call: ToolCall,
        result: ToolResult,
        started_at: datetime,
        ended_at: datetime,
    ) -> None: ...

    def trial_finished(
        self, identity: TrialIdentity, *, trajectory: Trajectory | None, error: str | None = None
    ) -> None:
        """The trial is over: ``trajectory`` carries status, grade and messages (``None`` when
        the trial died before producing one), ``error`` the exception that ended it, if any."""
        ...

    def trial_persisted(self, identity: TrialIdentity, *, trial_dir: Path) -> None:
        """The trial's bundle is on disk under ``trial_dir`` (after ``trial_finished``): a
        receiver-specific observer may attach the files to the trace (ADR-0046 amendment)."""
        ...

    def run_finished(self) -> ExportReceipt: ...


class NullTrialObserver:
    """The default: observes nothing, so callers never branch on ``observer is None``."""

    def trial_started(
        self, identity: TrialIdentity, *, models: Mapping[str, ModelRef], started_at: datetime
    ) -> None:
        return None

    def generation(self, identity: TrialIdentity, **_: Any) -> None:
        return None

    def tool_call(self, identity: TrialIdentity, **_: Any) -> None:
        return None

    def trial_finished(
        self, identity: TrialIdentity, *, trajectory: Trajectory | None, error: str | None = None
    ) -> None:
        return None

    def trial_persisted(self, identity: TrialIdentity, *, trial_dir: Path) -> None:
        return None

    def run_finished(self) -> ExportReceipt:
        return ExportReceipt()


@dataclass
class CompositeTrialObserver:
    """Fans every call out to several observers; one failing observer never starves another."""

    observers: Sequence[TrialObserver] = field(default_factory=tuple)

    def trial_started(self, identity: TrialIdentity, **kwargs: Any) -> None:
        for observer in self.observers:
            safely(observer.trial_started, identity, **kwargs)

    def generation(self, identity: TrialIdentity, **kwargs: Any) -> None:
        for observer in self.observers:
            safely(observer.generation, identity, **kwargs)

    def tool_call(self, identity: TrialIdentity, **kwargs: Any) -> None:
        for observer in self.observers:
            safely(observer.tool_call, identity, **kwargs)

    def trial_finished(self, identity: TrialIdentity, **kwargs: Any) -> None:
        for observer in self.observers:
            safely(observer.trial_finished, identity, **kwargs)

    def trial_persisted(self, identity: TrialIdentity, **kwargs: Any) -> None:
        for observer in self.observers:
            hook = getattr(observer, "trial_persisted", None)
            if callable(hook):
                safely(hook, identity, **kwargs)

    def run_finished(self) -> ExportReceipt:
        receipts = [safely(observer.run_finished) or ExportReceipt() for observer in self.observers]
        return ExportReceipt(
            spans_queued=sum(r.spans_queued for r in receipts),
            spans_exported=sum(r.spans_exported for r in receipts),
            spans_dropped=sum(r.spans_dropped for r in receipts),
            export_failures=sum(r.export_failures for r in receipts),
            flushed=all(r.flushed for r in receipts),
            exporter=", ".join(r.exporter for r in receipts if r.exporter != "none") or "none",
            attachments_registered=sum(r.attachments_registered for r in receipts),
            attachments_uploaded=sum(r.attachments_uploaded for r in receipts),
            attachments_deduplicated=sum(r.attachments_deduplicated for r in receipts),
            attachments_skipped=sum(r.attachments_skipped for r in receipts),
            attachments_failed=sum(r.attachments_failed for r in receipts),
            manifests_sent=sum(r.manifests_sent for r in receipts),
            manifests_failed=sum(r.manifests_failed for r in receipts),
        )


def safely(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run an observer call; a raising observer is logged and ignored (never fails a trial)."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the observability layer only warns
        _log.warning("trial observer %s failed: %s", getattr(fn, "__qualname__", fn), exc)
        return None


class LoopObserver(Protocol):
    """What :class:`~tolokaforge.core.loop.ToolCallingLoop` sees: one trial, one role, no ids."""

    def generation(
        self,
        *,
        index: int,
        turn: int,
        request: Sequence[Message],
        result: GenerationResult,
        started_at: datetime,
        ended_at: datetime,
    ) -> None: ...

    def tool_call(
        self,
        *,
        index: int,
        call: ToolCall,
        result: ToolResult,
        started_at: datetime,
        ended_at: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class LoopObserverBinding:
    """Binds a :class:`TrialObserver` to one trial and one role for the loop."""

    observer: TrialObserver
    identity: TrialIdentity
    role: str = "agent"

    def generation(self, **kwargs: Any) -> None:
        safely(self.observer.generation, self.identity, role=self.role, **kwargs)

    def tool_call(self, **kwargs: Any) -> None:
        safely(self.observer.tool_call, self.identity, role=self.role, **kwargs)
