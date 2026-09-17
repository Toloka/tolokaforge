"""OTLP exporter for the ``TrialObserver`` seam (ADR-0046). Needs the ``otel`` extra.

Spans are not opened through the SDK's tracer: each one is *synthesised* when its source event is
complete (a generation, a tool call, the graded trial) as a finished ``ReadableSpan`` with the id
contract's trace and span ids and the trial's own clocks, then handed to a bounded
:class:`SpanQueue` that exports in batches from a background thread. Parents are explicit, nothing
rides ambient context, and a full queue drops (counted) rather than blocking the agent loop.
Attribute names follow the Langfuse OpenTelemetry conventions so a Langfuse receiver renders the
same trace the offline uploader produces; any OTLP collector still receives valid spans.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from tolokaforge.core.redaction import SensitiveKeyRedaction
from tolokaforge.observability import ids as _ids
from tolokaforge.observability.attachments import AttachCounts
from tolokaforge.observability.langfuse_projection import (
    PROJECTION_FULL,
    PROJECTION_GRADINGS,
    ProjectionContext,
    build_projection,
)
from tolokaforge.observability.model_names import (
    NONE,
    ModelIdentity,
    ModelNameResolver,
    ModelNameResolverError,
    RawModelNameResolver,
)
from tolokaforge.observability.observer import ExportReceipt, ModelRef, TrialIdentity

_log = logging.getLogger(__name__)

HARNESS_TAG = "harness:tolokaforge"
TRACE_TIME_SOURCE = "live"


@dataclass(frozen=True)
class ProjectionSettings:
    """What the trial-end pass adds to a bundle (ADR-0046, parity amendment): the projection
    mode, the receiver's native fields, the deployment profile's mirrored prefixes and version,
    where each tag came from, and this producer's identity."""

    mode: str = PROJECTION_FULL  # full | gradings | none
    environment: str | None = None
    release: str | None = None
    version: str | None = None
    producer: str = "tolokaforge"  # the ``uploader_version`` metadata value
    tag_profile_version: str = NONE
    mirror_prefixes: tuple[str, ...] = ()
    tag_origins: Mapping[str, str] = field(default_factory=dict)


def _scope() -> InstrumentationScope:
    try:
        from importlib.metadata import version

        engine_version = version("tolokaforge")
    except Exception:  # noqa: BLE001 - version is informational
        engine_version = "unknown"
    return InstrumentationScope("tolokaforge.observability", engine_version)


def make_otlp_exporter(endpoint: str, headers: Mapping[str, str] | None = None) -> SpanExporter:
    """The standard OTLP/HTTP span exporter; ``OTEL_EXPORTER_OTLP_HEADERS`` supplies the
    receiver's credentials when ``headers`` is not given, and is never logged here."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint, headers=dict(headers) if headers else None)


class SpanQueue:
    """Bounded, counted, background-exported span queue (ADR-0046 delivery contract).

    ``put`` never blocks: a full queue drops the span and counts it. The worker exports batches
    as they fill or every ``interval_s``; :meth:`flush` drains synchronously in the caller's
    thread (bounded by the exporter's own timeout per batch); :meth:`shutdown` flushes, stops the
    worker and closes the exporter. Counters are read by :meth:`receipt`.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_size: int = 4096,
        batch_size: int = 64,
        interval_s: float = 1.0,
        name: str = "otlp",
    ) -> None:
        self._exporter = exporter
        self._max_size = max(1, max_size)
        self._batch_size = max(1, batch_size)
        self._interval_s = max(0.05, interval_s)
        self.name = name
        self._items: deque[ReadableSpan] = deque()
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.queued = 0
        self.exported = 0
        self.dropped = 0
        self.failures = 0
        self._thread = threading.Thread(
            target=self._worker, name="tolokaforge-otel-export", daemon=True
        )
        self._thread.start()

    def put(self, span: ReadableSpan) -> bool:
        with self._lock:
            if len(self._items) >= self._max_size:
                self.dropped += 1
                return False
            self._items.append(span)
            self.queued += 1
            ready = len(self._items) >= self._batch_size
        if ready:
            self._wake.set()
        return True

    def _take_batch(self) -> list[ReadableSpan]:
        with self._lock:
            count = min(self._batch_size, len(self._items))
            return [self._items.popleft() for _ in range(count)]

    def _export(self, batch: list[ReadableSpan]) -> None:
        try:
            result = self._exporter.export(batch)
        except Exception as exc:  # noqa: BLE001 - never propagate into the run
            _log.warning("span export raised: %s", exc)
            result = SpanExportResult.FAILURE
        with self._lock:
            if result is SpanExportResult.SUCCESS:
                self.exported += len(batch)
            else:
                self.failures += 1
                self.dropped += len(batch)

    def _drain(self, deadline: float | None = None) -> None:
        """Export batches until the queue is empty or ``deadline`` (``time.monotonic``) passes."""
        with self._drain_lock:
            while deadline is None or time.monotonic() < deadline:
                batch = self._take_batch()
                if not batch:
                    return
                self._export(batch)

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval_s)
            self._wake.clear()
            self._drain()
        self._drain()

    def flush(self, timeout_s: float | None = None) -> bool:
        """Export what is queued, in the caller's thread, within ``timeout_s``; whatever the budget
        did not cover is dropped and counted, so a receiver that is down cannot hold the run
        open. True when everything left."""
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        self._drain(deadline)
        with self._lock:
            left = len(self._items)
            if left:
                self._items.clear()
                self.dropped += left
        return left == 0

    def shutdown(self, timeout_s: float = 30.0) -> bool:
        """Flush within the budget, stop the worker, close the exporter; True when nothing was lost."""
        started = time.monotonic()
        flushed = self.flush(timeout_s)
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=max(0.1, timeout_s - (time.monotonic() - started)))
        try:
            self._exporter.shutdown()
        except Exception as exc:  # noqa: BLE001
            _log.warning("span exporter shutdown raised: %s", exc)
        return flushed and not self._thread.is_alive()

    def receipt(self, *, flushed: bool) -> ExportReceipt:
        return ExportReceipt(
            spans_queued=self.queued,
            spans_exported=self.exported,
            spans_dropped=self.dropped,
            export_failures=self.failures,
            flushed=flushed,
            exporter=self.name,
        )


class TrialAttachments(Protocol):
    """The post-trial step a receiver provides (``langfuse_media.LangfuseAttachments``): the
    file attachments, the ingestion route the trial-end events take, inline media, and the
    budget and breaker both share. ``attach_with_manifest``, ``register_media``, ``budget``,
    ``scan_events``, ``note_trial_outcome`` and ``tripped`` are optional capabilities looked up
    with ``getattr`` (a receiver with the bare ``attach`` / ``ingest`` pair still works)."""

    @property
    def mode(self) -> str: ...

    def ingest(self, events: list[dict[str, Any]], *, batch_size: int = 40) -> None: ...

    def attach(
        self,
        trace_id: str,
        trial_dir: Path,
        *,
        trace_timestamp: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AttachCounts: ...


@dataclass
class _PersistContext:
    """What a trial's end leaves for its ``trial_persisted`` pass, after the state is dropped."""

    started_at: datetime | None = None
    status: str | None = None
    judge_model: str | None = None
    user_model: str | None = None
    tags: tuple[str, ...] = ()


@dataclass
class _TrialState:
    identity: TrialIdentity
    started_at: datetime
    models: dict[str, ModelRef] = field(default_factory=dict)
    agent: ModelIdentity | None = None
    tags: tuple[str, ...] = ()
    generations: int = 0
    tool_calls: int = 0


class OTelTrialObserver:
    """Turns trial events into OTLP spans (see the module docstring for the shape)."""

    def __init__(
        self,
        *,
        queue: SpanQueue,
        resolver: ModelNameResolver | None = None,
        label: str,
        session_id: str,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        service_name: str = "tolokaforge",
        attribute_max_chars: int = 20_000,
        context_messages: int = 6,
        flush_timeout_s: float = 30.0,
        attachments: TrialAttachments | None = None,
        gradings: bool = True,
        expect_project: str | None = None,
        project_verified: str = "none",
        projection: ProjectionSettings | None = None,
    ) -> None:
        self._queue = queue
        self._attachments = attachments
        self._gradings = gradings
        self._projection = projection or ProjectionSettings()
        self._grading_counts = {"sent": 0, "failed": 0, "scores": 0, "users": 0}
        self._projection_counts = {
            "sent": 0,
            "failed": 0,
            "observations": 0,
            "events": 0,
            "media_uploaded": 0,
            "media_failed": 0,
        }
        self._expect_project = expect_project
        self._project_verified = project_verified
        self._attach_counts = AttachCounts()
        # trial start, final status, the judge / user model names and the tags by trace id, kept
        # from trial_finished to trial_persisted: the manifest update re-sends start and status,
        # so the trace keeps its own timestamp and ends with the trial's status even when the
        # receiver merged the provisional root's "running" last; the trial-end pass names the
        # models and re-sends the tags
        self._persist: dict[str, _PersistContext] = {}
        self._resolver: ModelNameResolver = resolver or RawModelNameResolver()
        self._label = label
        self._session_id = session_id
        self._tags = tuple(tags)
        self._metadata = dict(metadata or {})
        self._resource = Resource.create({"service.name": service_name})
        self._scope = _scope()
        self._max_chars = max(64, attribute_max_chars)
        self._context_messages = max(1, context_messages)
        self._flush_timeout_s = flush_timeout_s
        self._redaction = SensitiveKeyRedaction()
        self._states: dict[str, _TrialState] = {}
        self._states_lock = threading.Lock()

    # -- TrialObserver ---------------------------------------------------------------------------

    def trial_started(
        self, identity: TrialIdentity, *, models: Mapping[str, ModelRef], started_at: datetime
    ) -> None:
        agent_ref = models.get("agent")
        agent = self._resolve(agent_ref) if agent_ref is not None else None
        tags: list[str] = [HARNESS_TAG]
        if agent is not None:
            tags.extend(agent.tags)
        # the task is a fact of the trial the producer owns (tag vocabulary v2, core `task:`)
        tags.append(f"task:{identity.task_id}")
        for tag in self._tags:
            if tag not in tags:
                tags.append(tag)
        state = _TrialState(
            identity=identity,
            started_at=started_at,
            models=dict(models),
            agent=agent,
            tags=tuple(tags),
        )
        with self._states_lock:
            self._states[identity.trace_id] = state
        # The root span goes out now, open-ended (end = start) and marked running, and again at
        # the end with everything it knows: the receiver dates the trace from the first span it
        # sees, which would otherwise be the first generation, seconds after the trial started.
        attributes = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "span",
            "langfuse.trace.metadata.task_id": identity.task_id,
            "langfuse.trace.metadata.trial_index": identity.trial_index,
            "langfuse.trace.metadata.attempt": identity.attempt_id,
            "langfuse.trace.metadata.run_id": identity.run_id,
            "langfuse.trace.metadata.run_tag": identity.run_tag,
            "langfuse.trace.metadata.status": "running",
            "langfuse.trace.metadata.trace_time_source": TRACE_TIME_SOURCE,
        }
        self._emit(
            name=f"trial {identity.task_id}/{identity.trial_index}",
            identity=identity,
            span_id=identity.root_id,
            parent_id=None,
            attributes=attributes,
            start=started_at,
            end=started_at,
        )

    def generation(
        self,
        identity: TrialIdentity,
        *,
        role: str,
        index: int,
        turn: int,
        request: Sequence[Any],
        result: Any,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        state = self._state(identity)
        state.generations += 1
        # a non-agent generation is a judge turn of the run's own grading (contract v2: kind
        # ``jgen`` under the grading id ``live:<run_id>``)
        agent_role = role == "agent"
        name = f"assistant turn {index}" if agent_role else f"judge turn {index}"
        usage = getattr(result, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        details: dict[str, int] = {
            "input": max(0, prompt - cache_read),
            "output": completion,
            "total": prompt + completion,
        }
        if cache_read:
            details["cache_read_input_tokens"] = cache_read
        if cache_creation:
            details["cache_creation_input_tokens"] = cache_creation
        model_name = (
            state.agent.canonical
            if (role == "agent" and state.agent)
            else self._model_name_of(state, role)
        )
        attributes: dict[str, Any] = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "generation",
            "langfuse.observation.level": "DEFAULT",
            "langfuse.observation.input": self._json(
                [self._message_dict(m) for m in list(request)[-self._context_messages :]]
            ),
            "langfuse.observation.output": self._json(self._result_dict(result)),
            "langfuse.observation.usage_details": self._json(details),
            "gen_ai.usage.input_tokens": prompt,
            "gen_ai.usage.output_tokens": completion,
            "langfuse.observation.metadata.turn": turn,
            "langfuse.observation.metadata.role": role,
            "langfuse.observation.metadata.message_index": index,
            "langfuse.observation.metadata.latency_s": float(
                getattr(result, "latency_s", 0.0) or 0.0
            ),
            "langfuse.observation.metadata.finish_reason": str(
                getattr(result, "finish_reason", None) or NONE
            ),
            "langfuse.observation.metadata.openrouter_generation_id": str(
                getattr(result, "openrouter_generation_id", None) or NONE
            ),
            "langfuse.observation.metadata.reasoning_tokens": int(
                getattr(usage, "reasoning_tokens", 0) or 0
            ),
            "langfuse.observation.metadata.cached_tokens": int(
                getattr(usage, "cached_tokens", 0) or 0
            ),
            "langfuse.observation.metadata.usage_source": "exact",
        }
        if model_name:
            attributes["langfuse.observation.model.name"] = model_name
            attributes["gen_ai.request.model"] = model_name
        cost = getattr(result, "cost_usd", None)
        if cost is not None:
            attributes["langfuse.observation.cost_details"] = self._json({"total": float(cost)})
        self._emit(
            name=name,
            identity=identity,
            span_id=(
                identity.observation_id("gen", index)
                if agent_role
                else identity.observation_id("jgen", f"live:{identity.run_id}", index)
            ),
            parent_id=identity.root_id,
            attributes=attributes,
            start=started_at,
            end=ended_at,
        )

    def tool_call(
        self,
        identity: TrialIdentity,
        *,
        role: str,
        index: int,
        call: Any,
        result: Any,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        state = self._state(identity)
        state.tool_calls += 1
        tool_name = str(getattr(call, "name", "tool"))
        # contract v2: a tool execution is keyed by the episode-unique call id the loop assigned
        # (the same value the bundle's tool_log.yaml and tool message carry), never by position
        key = _ids.tool_key(getattr(call, "id", None), index)
        span_id = (
            identity.observation_id("tool", key)
            if role == "agent"
            else identity.observation_id("jtool", f"live:{identity.run_id}", key)
        )
        name = f"tool: {tool_name}" if role == "agent" else f"judge tool: {tool_name}"
        success = bool(getattr(result, "success", True))
        output = (
            getattr(result, "output", "") if success else f"Error: {getattr(result, 'error', '')}"
        )
        attributes: dict[str, Any] = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "span",
            "langfuse.observation.level": "DEFAULT" if success else "ERROR",
            "langfuse.observation.input": self._json(
                self._redact(getattr(call, "arguments", None) or {})
            ),
            "langfuse.observation.output": self._cap(str(output)),
            "langfuse.observation.metadata.tool": tool_name,
            "langfuse.observation.metadata.call_id": str(getattr(call, "id", None) or NONE),
            "langfuse.observation.metadata.role": role,
            "langfuse.observation.metadata.message_index": index,
            "langfuse.observation.metadata.success": success,
            "langfuse.observation.metadata.duration_s": float(
                getattr(result, "duration_s", 0.0) or 0.0
            ),
        }
        status = getattr(result, "status", None)
        if status is not None:
            attributes["langfuse.observation.metadata.status"] = _enum_value(status)
        if not success:
            attributes["langfuse.observation.status_message"] = self._cap(
                str(getattr(result, "error", ""))
            )
        self._emit(
            name=name,
            identity=identity,
            span_id=span_id,
            parent_id=identity.root_id,
            attributes=attributes,
            start=started_at,
            end=ended_at,
            error=not success,
        )

    def trial_finished(
        self, identity: TrialIdentity, *, trajectory: Any, error: str | None = None
    ) -> None:
        """Close the trace with the root span. ``trajectory`` is ``None`` when the trial died
        before producing one; ``error`` names the exception that ended it, if any."""
        with self._states_lock:
            state = self._states.pop(identity.trace_id, None)
        if state is None:
            state = _TrialState(
                identity=identity,
                started_at=_as_utc(getattr(trajectory, "start_ts", None))
                or datetime.now(tz=timezone.utc),
            )
        grade = getattr(trajectory, "grade", None)
        metrics = getattr(trajectory, "metrics", None)
        usage = getattr(metrics, "usage", None)
        messages = list(getattr(trajectory, "messages", []) or [])
        status = getattr(trajectory, "status", None)
        termination = getattr(trajectory, "termination_reason", None)
        metadata: dict[str, Any] = {
            **self._metadata,  # caller keys first: the exporter's own keys win on a clash
            "task_id": identity.task_id,
            "trial_index": identity.trial_index,
            "attempt": identity.attempt_id,
            "run_id": identity.run_id,
            "run_tag": identity.run_tag,
            "label": self._label,
            "harness": "tolokaforge",
            "trace_time_source": TRACE_TIME_SOURCE,
            "status": _enum_value(status) if trajectory is not None else "error",
            "termination_reason": _enum_value(termination),
            "error": error or NONE,
            "grading_error": str(getattr(trajectory, "grading_error", None) or NONE),
            "pass": getattr(grade, "binary_pass", None) if grade is not None else NONE,
            "score": getattr(grade, "score", None) if grade is not None else NONE,
            "turns": int(getattr(metrics, "turns", 0) or 0),
            "api_calls": int(getattr(metrics, "api_calls", 0) or 0),
            "tool_calls": int(getattr(metrics, "tool_calls", 0) or 0),
            "tokens_input": int(getattr(usage, "prompt_tokens", 0) or 0),
            "tokens_output": int(getattr(usage, "completion_tokens", 0) or 0),
            "cost_usd": getattr(metrics, "cost_usd", None) if metrics is not None else None,
            "generations_observed": state.generations,
            "tool_calls_observed": state.tool_calls,
        }
        agent_ref = state.models.get("agent")
        metadata["model_name"] = state.agent.canonical if state.agent else NONE
        metadata["model_provider"] = (
            agent_ref.provider if agent_ref and agent_ref.provider else NONE
        )
        if state.agent is not None:
            metadata.update(state.agent.metadata)
        for role, ref in state.models.items():
            if role != "agent":
                metadata[f"{role}_model"] = (
                    ref.name if "/" in ref.name else f"{ref.provider}/{ref.name}"
                )
        first_user = next((m for m in messages if _role_of(m) == "user"), None)
        last_assistant = next(
            (m for m in reversed(messages) if _role_of(m) == "assistant" and _content(m)),
            None,
        )
        attributes: dict[str, Any] = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "span",
            "langfuse.trace.input": self._cap(_content(first_user)),
            "langfuse.trace.output": self._cap(_content(last_assistant)),
        }
        for key, value in metadata.items():
            attributes[f"langfuse.trace.metadata.{key}"] = _attribute_value(value)
        start = state.started_at or _as_utc(getattr(trajectory, "start_ts", None))
        end = _as_utc(getattr(trajectory, "end_ts", None)) or datetime.now(tz=timezone.utc)
        status_value = _enum_value(status) if trajectory is not None else "error"
        with self._states_lock:
            self._persist[identity.trace_id] = _PersistContext(
                started_at=start,
                status=status_value,
                judge_model=self._model_name_of(state, "judge"),
                user_model=self._model_name_of(state, "user"),
                tags=state.tags,
            )
        self._emit(
            name=f"trial {identity.task_id}/{identity.trial_index}",
            identity=identity,
            span_id=identity.root_id,
            parent_id=None,
            attributes=attributes,
            start=start,
            end=end,
            error=error is not None or status_value in {"error", "failed", "timeout"},
        )

    def trial_persisted(self, identity: TrialIdentity, *, trial_dir: Path) -> None:
        """The bundle is on disk: attach its files to the trace, write manifest v2 and complete
        the trace from the bundle (the default projection, or the gradings alone, or nothing,
        per ``ProjectionSettings.mode``), all under one budget and breaker. Runs in the trial's
        own thread, bounded by the step's timeouts, never raises; the counts land in the
        receipt."""
        with self._states_lock:
            persist = self._persist.pop(identity.trace_id, None) or _PersistContext()
            state = self._states.get(identity.trace_id)  # a trial persisted without finishing
        if state is not None:
            persist.judge_model = persist.judge_model or self._model_name_of(state, "judge")
            persist.user_model = persist.user_model or self._model_name_of(state, "user")
            persist.tags = persist.tags or state.tags
        if self._attachments is None:
            return
        step = self._attachments
        budget = getattr(step, "budget", None)
        scope = budget() if callable(budget) else nullcontext()
        with scope:
            manifest: dict[str, Any] | None = None
            if getattr(step, "mode", "all") != "none":
                with_manifest = getattr(step, "attach_with_manifest", None)
                if callable(with_manifest):
                    counts, manifest = with_manifest(
                        identity.trace_id,
                        Path(trial_dir),
                        trace_timestamp=persist.started_at,
                        metadata={"status": persist.status} if persist.status else None,
                    )
                else:
                    counts = step.attach(
                        identity.trace_id,
                        Path(trial_dir),
                        trace_timestamp=persist.started_at,
                        metadata={"status": persist.status} if persist.status else None,
                    )
                with self._states_lock:
                    self._attach_counts.add(counts)
            if self._projection.mode == PROJECTION_FULL:
                self._send_projection(identity, Path(trial_dir), persist, manifest)
            elif self._projection.mode == PROJECTION_GRADINGS and self._gradings:
                self._send_gradings(
                    identity, Path(trial_dir), persist.judge_model, persist.user_model
                )

    def _trial_tags(self, identity: TrialIdentity, persist: _PersistContext) -> tuple[str, ...]:
        """The trace's tags for the trial-end pass: what the trial started with, else rebuilt
        from what the observer knows (a trial persisted without a recorded start)."""
        if persist.tags:
            return persist.tags
        tags: list[str] = [HARNESS_TAG, f"task:{identity.task_id}"]
        for tag in self._tags:
            if tag not in tags:
                tags.append(tag)
        return tuple(tags)

    def _send_projection(
        self,
        identity: TrialIdentity,
        trial_dir: Path,
        persist: _PersistContext,
        manifest: Mapping[str, Any] | None,
    ) -> None:
        """The persisted bundle's default projection (``langfuse_projection``): the same
        records the offline connector writes, under the contract ids, through the ingestion
        API. A bundle that cannot be projected, a data-safety hit over the serialised events
        or a refused batch is a counted failure, never an exception."""
        step = self._attachments
        assert step is not None
        if getattr(step, "tripped", False):
            with self._states_lock:
                self._projection_counts["failed"] += 1
            return
        settings = self._projection
        context = ProjectionContext(
            label=self._label,
            session_id=self._session_id,
            tags=self._trial_tags(identity, persist),
            tag_origins=dict(settings.tag_origins),
            metadata=dict(self._metadata),
            mirror_prefixes=settings.mirror_prefixes,
            environment=settings.environment,
            release=settings.release,
            version=settings.version,
            producer=settings.producer,
            tag_profile_version=settings.tag_profile_version,
            project_verified=self._project_verified,
            attach_mode=str(getattr(step, "mode", "all")),
            grades=self._gradings,
        )
        media = getattr(step, "register_media", None)
        try:
            projection = build_projection(
                identity,
                trial_dir,
                context,
                resolver=self._resolver,
                manifest=manifest,
                media=media if callable(media) else None,
            )
        except Exception as exc:  # noqa: BLE001 - a malformed bundle is not the trace's problem
            _log.warning(
                "projection: bundle of trace %s not projected: %s",
                identity.trace_id,
                type(exc).__name__,
            )
            with self._states_lock:
                self._projection_counts["failed"] += 1
            return
        scan = getattr(step, "scan_events", None)
        try:
            findings = scan(projection.events) if callable(scan) else []
        except Exception as exc:  # noqa: BLE001 - an unserialisable body is a failed pass
            _log.warning(
                "projection: trace %s not scanned: %s", identity.trace_id, type(exc).__name__
            )
            with self._states_lock:
                self._projection_counts["failed"] += 1
            return
        if findings:
            # the outbound data-safety gate: nothing is rewritten, the pass is not sent
            _log.warning(
                "projection: trace %s not sent, the events would carry a secret: %s",
                identity.trace_id,
                ", ".join(findings),
            )
            with self._states_lock:
                self._projection_counts["failed"] += 1
            return
        note = getattr(step, "note_trial_outcome", None)
        try:
            step.ingest(projection.events)
        except Exception as exc:  # noqa: BLE001 - the observability layer only warns
            reason = str(exc) if type(exc).__name__ == "LangfuseApiError" else type(exc).__name__
            _log.warning("projection: trace %s not sent: %s", identity.trace_id, reason)
            with self._states_lock:
                self._projection_counts["failed"] += 1
            if callable(note) and getattr(step, "mode", "all") == "none":
                note(reached=False)
            return
        if callable(note) and getattr(step, "mode", "all") == "none":
            note(reached=True)
        stats = projection.stats
        with self._states_lock:
            self._projection_counts["sent"] += 1
            self._projection_counts["observations"] += stats.observations
            self._projection_counts["events"] += stats.events
            self._projection_counts["media_uploaded"] += stats.media_uploaded
            self._projection_counts["media_failed"] += stats.media_failed
            if stats.grading_id:
                self._grading_counts["sent"] += 1
            self._grading_counts["scores"] += stats.scores
            self._grading_counts["users"] += stats.user_generations

    def _send_gradings(
        self,
        identity: TrialIdentity,
        trial_dir: Path,
        judge_name: str | None,
        user_name: str | None,
    ) -> None:
        """The bundle's grading, judge transcript, scores and simulated user turns as ingestion
        events under the shared id contract (``langfuse_gradings``; the ``gradings`` projection
        mode). Never raises."""
        from tolokaforge.observability.langfuse_gradings import build_grading_events

        try:
            built = build_grading_events(
                identity.trace_id,
                trial_dir,
                run_id=identity.run_id,
                judge_model_name=judge_name,
                user_model_name=user_name,
            )
        except Exception as exc:  # noqa: BLE001 - a malformed bundle is not the trace's problem
            _log.warning(
                "gradings: bundle of trace %s not read: %s", identity.trace_id, type(exc).__name__
            )
            with self._states_lock:
                self._grading_counts["failed"] += 1
            return
        if not built.events:
            return
        if self._projection.environment is not None:
            for event in built.events:
                event["body"].setdefault("environment", self._projection.environment)
        try:
            self._attachments.ingest(built.events)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - the observability layer only warns
            reason = str(exc) if type(exc).__name__ == "LangfuseApiError" else type(exc).__name__
            _log.warning("gradings: trace %s not sent: %s", identity.trace_id, reason)
            with self._states_lock:
                self._grading_counts["failed"] += 1
            return
        with self._states_lock:
            if built.grading_id:
                self._grading_counts["sent"] += 1
            self._grading_counts["scores"] += built.scores
            self._grading_counts["users"] += built.user_generations

    def run_finished(self) -> ExportReceipt:
        flushed = self._queue.shutdown(self._flush_timeout_s)
        with self._states_lock:
            self._persist.clear()  # trials that were never announced
        counts = self._attach_counts
        return replace(
            self._queue.receipt(flushed=flushed),
            attachments_registered=counts.registered,
            attachments_uploaded=counts.uploaded,
            attachments_deduplicated=counts.deduplicated,
            attachments_skipped=counts.skipped,
            attachments_failed=counts.failed,
            manifests_sent=counts.manifests_sent,
            manifests_failed=counts.manifests_failed,
            expect_project=self._expect_project,
            project_verified=self._project_verified,
            gradings_sent=self._grading_counts["sent"],
            gradings_failed=self._grading_counts["failed"],
            scores_sent=self._grading_counts["scores"],
            user_generations_sent=self._grading_counts["users"],
            projections_sent=self._projection_counts["sent"],
            projections_failed=self._projection_counts["failed"],
            observations_sent=self._projection_counts["observations"],
            events_sent=self._projection_counts["events"],
            media_uploaded=self._projection_counts["media_uploaded"],
            media_failed=self._projection_counts["media_failed"],
        )

    # -- helpers ------------------------------------------------------------------------------------

    def _resolve(self, ref: ModelRef) -> ModelIdentity:
        try:
            return self._resolver.resolve(ref.provider, ref.name)
        except ModelNameResolverError as exc:
            _log.warning("model name not resolved, raw identity used: %s", exc)
            return RawModelNameResolver().resolve(ref.provider, ref.name)

    def _state(self, identity: TrialIdentity) -> _TrialState:
        with self._states_lock:
            state = self._states.get(identity.trace_id)
            if state is None:
                state = _TrialState(
                    identity=identity,
                    started_at=datetime.now(tz=timezone.utc),
                    tags=(HARNESS_TAG, *self._tags),
                )
                self._states[identity.trace_id] = state
            return state

    def _model_name_of(self, state: _TrialState, role: str) -> str | None:
        ref = state.models.get(role)
        if ref is None:
            return None
        return self._resolve(ref).canonical

    def _trace_attributes(self, state: _TrialState) -> dict[str, Any]:
        # the receiver's native fields ride on every span: Langfuse fixes a trace's environment
        # at the first write it sees (verified on the instance 2026-09-17), so the provisional
        # root span already carries it and a trace never has to be repaired afterwards
        attributes: dict[str, Any] = {
            "langfuse.trace.name": f"{self._label}/{state.identity.task_id}",
            "langfuse.session.id": self._session_id,
            "langfuse.trace.tags": list(state.tags),
        }
        settings = self._projection
        if settings.environment is not None:
            attributes["langfuse.environment"] = settings.environment
        if settings.release is not None:
            attributes["langfuse.release"] = settings.release
        if settings.version is not None:
            attributes["langfuse.version"] = settings.version
        return attributes

    def _emit(
        self,
        *,
        name: str,
        identity: TrialIdentity,
        span_id: str,
        parent_id: str | None,
        attributes: Mapping[str, Any],
        start: datetime | None,
        end: datetime | None,
        error: bool = False,
    ) -> None:
        trace_int = int(identity.trace_id, 16)
        context = SpanContext(
            trace_id=trace_int,
            span_id=int(span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        parent = (
            SpanContext(
                trace_id=trace_int,
                span_id=int(parent_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            if parent_id
            else None
        )
        end_dt = _as_utc(end) or datetime.now(tz=timezone.utc)
        start_dt = _as_utc(start) or end_dt
        if start_dt > end_dt:
            start_dt = end_dt
        span = ReadableSpan(
            name=name,
            context=context,
            parent=parent,
            resource=self._resource,
            attributes={k: v for k, v in attributes.items() if v is not None},
            events=(),
            links=(),
            kind=SpanKind.INTERNAL,
            instrumentation_scope=self._scope,
            status=Status(StatusCode.ERROR) if error else Status(StatusCode.UNSET),
            start_time=_nanos(start_dt),
            end_time=_nanos(end_dt),
        )
        self._queue.put(span)

    def _redact(self, mapping: Any) -> Any:
        if isinstance(mapping, Mapping):
            return self._redaction.redact_mapping(mapping)
        return mapping

    def _message_dict(self, message: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": _role_of(message),
            "content": self._cap(_content(message)),
        }
        calls = getattr(message, "tool_calls", None)
        if calls:
            out["tool_calls"] = [
                {
                    "id": getattr(c, "id", None),
                    "name": getattr(c, "name", None),
                    "arguments": self._redact(getattr(c, "arguments", None) or {}),
                }
                for c in calls
            ]
        if getattr(message, "tool_call_id", None):
            out["tool_call_id"] = message.tool_call_id
        blocks = getattr(message, "content_blocks", None)
        if blocks:
            out["content_blocks_omitted"] = len(
                blocks
            )  # media never leaves the machine through spans
        return out

    def _result_dict(self, result: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"content": self._cap(str(getattr(result, "text", None) or ""))}
        calls = getattr(result, "tool_calls", None)
        if calls:
            out["tool_calls"] = [
                {
                    "id": getattr(c, "id", None),
                    "name": getattr(c, "name", None),
                    "arguments": self._redact(getattr(c, "arguments", None) or {}),
                }
                for c in calls
            ]
        return out

    def _json(self, value: Any) -> str:
        return self._cap(json.dumps(value, ensure_ascii=False, default=str))

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[: self._max_chars - 16] + f"...[+{len(text) - self._max_chars + 16}]"


def _content(message: Any) -> str:
    """A message's text content, empty for None or a missing attribute."""
    return str(getattr(message, "content", None) or "")


def _role_of(message: Any) -> str:
    role = getattr(message, "role", None)
    return _enum_value(role) if role is not None else ""


def _enum_value(value: Any) -> str:
    """``Enum.value`` as text, a plain value as text, ``none`` for nothing."""
    if value is None:
        return NONE
    inner = getattr(value, "value", None)
    return str(inner if inner is not None else value)


def _attribute_value(value: Any) -> Any:
    if value is None:
        return NONE
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _nanos(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)
