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

    def observation_id(self, kind: str, index: int) -> str:
        return ids.observation_id(self.trace_id, kind, index)


@dataclass(frozen=True)
class ExportReceipt:
    """What left the process: the counts a run summary reports (ADR-0046, delivery contract)."""

    spans_queued: int = 0
    spans_exported: int = 0
    spans_dropped: int = 0
    export_failures: int = 0
    flushed: bool = True
    exporter: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "spans_queued": self.spans_queued,
            "spans_exported": self.spans_exported,
            "spans_dropped": self.spans_dropped,
            "export_failures": self.export_failures,
            "flushed": self.flushed,
            "exporter": self.exporter,
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

    def trial_finished(self, identity: TrialIdentity, *, trajectory: Trajectory) -> None: ...

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

    def trial_finished(self, identity: TrialIdentity, *, trajectory: Trajectory) -> None:
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

    def run_finished(self) -> ExportReceipt:
        receipts = [safely(observer.run_finished) or ExportReceipt() for observer in self.observers]
        return ExportReceipt(
            spans_queued=sum(r.spans_queued for r in receipts),
            spans_exported=sum(r.spans_exported for r in receipts),
            spans_dropped=sum(r.spans_dropped for r in receipts),
            export_failures=sum(r.export_failures for r in receipts),
            flushed=all(r.flushed for r in receipts),
            exporter=", ".join(r.exporter for r in receipts if r.exporter != "none") or "none",
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
