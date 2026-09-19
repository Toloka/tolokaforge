"""The ``TrialObserver`` seam (ADR-0047).

The conductor opens and closes a trial, the tool-calling loop reports each generation and tool
call with its content, the orchestrator closes the run. Every implementation is called through
:func:`safely`, so ordinary observer exceptions are logged without failing the trial. Parents are explicit
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

from pydantic import BaseModel, Field, NonNegativeInt

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


class ExportReceipt(BaseModel):
    """Process-local delivery counts, with opaque plugin details (ADR-0047).

    Plugins namespace their additive ``extra`` counters (for example,
    ``langfuse.attachments_uploaded``). ``details`` preserves each receiver's
    non-additive facts separately when several observers are composed.
    """

    model_config = {"extra": "forbid", "frozen": True}

    spans_queued: NonNegativeInt = 0
    spans_exported: NonNegativeInt = 0
    spans_dropped: NonNegativeInt = 0
    export_failures: NonNegativeInt = 0
    flushed: bool = True
    exporter: str = "none"
    extra: dict[str, NonNegativeInt] = Field(default_factory=dict)
    details: tuple[dict[str, str | None], ...] = ()

    @classmethod
    def merge(cls, receipts: Sequence[ExportReceipt]) -> ExportReceipt:
        """Sum disjoint process/observer receipts; preserve all receiver details.

        Counts describe export attempts, not unique receiver records. Merging the
        same process receipt twice double-counts it; callers own deduplication.
        """
        extra: dict[str, int] = {}
        for receipt in receipts:
            for key, value in receipt.extra.items():
                extra[key] = extra.get(key, 0) + value
        return cls(
            spans_queued=sum(r.spans_queued for r in receipts),
            spans_exported=sum(r.spans_exported for r in receipts),
            spans_dropped=sum(r.spans_dropped for r in receipts),
            export_failures=sum(r.export_failures for r in receipts),
            flushed=all(r.flushed for r in receipts),
            exporter=", ".join(r.exporter for r in receipts if r.exporter != "none") or "none",
            extra=extra,
            details=tuple(detail for receipt in receipts for detail in receipt.details),
        )


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
        receiver-specific observer may attach the files to the trace (ADR-0047 amendment)."""
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
class TrialObserverCallLog:
    """Hook order and arguments received by an in-memory observer, including failed calls."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class InMemoryTrialObserver:
    """Deterministic observer for conductor/orchestrator injection (ADR-0011).

    ``fail_on`` maps hook names to exceptions raised after recording the call;
    ``receipt`` controls the result of a successful ``run_finished``.
    """

    def __init__(
        self,
        *,
        receipt: ExportReceipt | None = None,
        fail_on: Mapping[str, Exception] | None = None,
    ) -> None:
        self.call_log = TrialObserverCallLog()
        self.receipt = receipt if receipt is not None else ExportReceipt()
        self.fail_on = dict(fail_on or {})

    def _record(self, name: str, **kwargs: Any) -> None:
        self.call_log.calls.append((name, kwargs))
        if name in self.fail_on:
            raise self.fail_on[name]

    def trial_started(
        self, identity: TrialIdentity, *, models: Mapping[str, ModelRef], started_at: datetime
    ) -> None:
        self._record("trial_started", identity=identity, models=models, started_at=started_at)

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
    ) -> None:
        self._record(
            "generation",
            identity=identity,
            role=role,
            index=index,
            turn=turn,
            request=request,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
        )

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
    ) -> None:
        self._record(
            "tool_call",
            identity=identity,
            role=role,
            index=index,
            call=call,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
        )

    def trial_finished(
        self, identity: TrialIdentity, *, trajectory: Trajectory | None, error: str | None = None
    ) -> None:
        self._record("trial_finished", identity=identity, trajectory=trajectory, error=error)

    def trial_persisted(self, identity: TrialIdentity, *, trial_dir: Path) -> None:
        self._record("trial_persisted", identity=identity, trial_dir=trial_dir)

    def run_finished(self) -> ExportReceipt:
        self._record("run_finished")
        return self.receipt


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
        receipts = []
        for observer in self.observers:
            receipt = safely(observer.run_finished)
            if receipt is None:
                receipt = ExportReceipt(export_failures=1, flushed=False)
            receipts.append(receipt)
        return ExportReceipt.merge(receipts)


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
