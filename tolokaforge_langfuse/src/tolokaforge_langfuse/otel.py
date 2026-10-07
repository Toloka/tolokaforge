"""OTLP exporter for the ``TrialObserver`` seam (ADR-0047). Needs the ``otel`` extra.

Spans are not opened through the SDK's tracer: each one is *synthesised* when its source event is
complete (a generation, a tool call, the graded trial) as a finished ``ReadableSpan`` with the id
contract's trace and span ids and the trial's own clocks, then handed to a bounded
:class:`SpanQueue` that exports in batches from a background thread. Parents are explicit, nothing
rides ambient context, and a full queue drops (counted) rather than blocking the agent loop.
Attribute names follow the Langfuse OpenTelemetry conventions so a Langfuse receiver renders the
same trace the offline uploader produces; any OTLP collector still receives valid spans.

Every live span is scanned by the data-safety gate before it is queued: one that would carry a
secret is withheld and counted, never rewritten (docs/OBSERVABILITY.md, "Delivery").

**Two write shapes, chosen by the receiver's family** (detected once per run and passed in as
``server_api``):

- ``v3``: what this module always did. The per-call spans carry the contract's final ids, the
  root is provisional at trial start and complete at trial end, and the trial-end pass re-sends
  every record through the ingestion API, where the receiver upserts.
- ``v4``: the producer writes every id **once** by policy. At ``trial_persisted`` the bundle's
  projection is converted to spans by :mod:`tolokaforge_langfuse.otlp_spans` and written once,
  **the root last**, after the media upload, with the complete manifest in the root's metadata;
  the scores keep the ingestion route. Nothing goes out while the trial runs unless the run asks
  for live **previews** (``previews``, the plugin's ``LANGFUSE_TRACING_PREVIEWS``): then the live
  spans go out under the preview kinds, children of a preview root whose parent is the final
  root, marked ``preview: true`` and named ``preview: ...``, and stay beside the final rows;
  nothing live is ever re-sent or completed. A trial whose final root can no longer come gets
  one minimal error root at ``run_finished``, so no trace is left without a root.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
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
from tolokaforge.observability.observer import ExportReceipt, ModelRef, TrialIdentity
from tolokaforge_langfuse.attachments import AttachCounts
from tolokaforge_langfuse.costs import call_cost, call_record

# the receiver families this observer writes for: `media` owns the names because the capability
# probe lives there, and a run resolves its family there once, never from the version a receiver
# reports
from tolokaforge_langfuse.media import SERVER_V3, SERVER_V4
from tolokaforge_langfuse.model_names import (
    NONE,
    ModelIdentity,
    ModelNameResolver,
    ModelNameResolverError,
    RawModelNameResolver,
)
from tolokaforge_langfuse.otlp_spans import (
    IDENTITY_METADATA_KEYS,
    OBSERVATION_METADATA_PREFIX,
    TRACE_METADATA_PREFIX,
    score_events,
    spans_from_events,
)
from tolokaforge_langfuse.projection import (
    PROJECTION_FULL,
    PROJECTION_GRADINGS,
    ProjectionContext,
    build_projection,
)
from tolokaforge_langfuse.retry import Retrier, RetryStats
from tolokaforge_langfuse.safety import Finding, SafetyGate, strings_in
from tolokaforge_langfuse.vocabulary import (
    ALL_DERIVED_GROUPS,
    NAME_AGENT,
    NAME_JUDGE,
    NAME_TRIAL,
    SOURCE_TRIAL,
    TRACE_USER_NONE,
    trace_name,
    trace_user,
)

_log = logging.getLogger(__name__)

HARNESS_TAG = "harness:tolokaforge"
# a trial observer traces trials: the source is the producer's fact (vocabulary.SOURCE_TRIAL)
SOURCE_TAG = f"source:{SOURCE_TRIAL}"
TRACE_TIME_SOURCE = "live"
# what a preview row says about itself: a marker in its metadata and a name a reader recognises
# without looking the id up
PREVIEW_METADATA_KEY = "preview"
PREVIEW_NAME_PREFIX = "preview: "
# how often a flush looks again while the worker still exports a batch
FLUSH_POLL_S = 0.05


@dataclass(frozen=True)
class ProjectionSettings:
    """What the trial-end pass adds to a bundle (ADR-0047, parity amendment): the projection
    mode, the receiver's native fields, the deployment profile's mirrored prefixes and version,
    where each tag came from, and this producer's identity."""

    mode: str = PROJECTION_FULL  # full | gradings | none
    environment: str | None = None
    release: str | None = None
    version: str | None = None
    producer: str = "tolokaforge"  # the ``uploader_version`` metadata value
    # the groups of bundle-derived tags the trial-end pass adds (vocabulary.DERIVED_GROUPS)
    derived_groups: frozenset[str] = ALL_DERIVED_GROUPS
    # the profile's ``[trace]``: the name template and the source of the trace's user
    trace_name: str | None = None
    trace_user: str = TRACE_USER_NONE


def _scope() -> InstrumentationScope:
    try:
        from importlib.metadata import version

        engine_version = version("tolokaforge")
    except Exception:  # noqa: BLE001 - version is informational
        engine_version = "unknown"
    return InstrumentationScope("tolokaforge.observability", engine_version)


class SpanQueue:
    """Bounded, counted, background-exported span queue (ADR-0047 delivery contract).

    ``put`` never blocks: a full queue drops the span and counts it. The worker exports batches
    as they fill or every ``interval_s``; :meth:`flush` drains in the caller's thread within its
    timeout, and waits for a batch the worker is exporting no longer than that; :meth:`shutdown`
    flushes, stops the worker and closes the exporter. Counters are read by :meth:`receipt`.

    With the exporter's ``retrier`` (:mod:`tolokaforge_langfuse.retry`) a flush during which the
    receiver refuses with an answer the retry policy waits out (a batch already waiting one out
    when the flush starts included) goes on for up to ``flush_grace_s`` more, in total over all
    the queue's flushes, and no wait of the exporter's outlasts it. The extension ends once a
    batch fails in another way (a timeout, a lost connection, a status the policy does not wait
    out) or the retry breaker opens. A flush without such a refusal ends at its timeout as
    before, so a receiver that is down holds the run no longer than it did. ``clock`` must be
    the retrier's.

    A span may be handed a ``track`` key: the queue then reports whether that span reached the
    exporter (:meth:`lost_tracked`). Only the write-once roots are tracked, so the set stays one
    entry per trial, and a root that never arrived can still be answered with an error root
    before the run ends.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_size: int = 4096,
        batch_size: int = 64,
        interval_s: float = 1.0,
        name: str = "otlp",
        retrier: Retrier | None = None,
        flush_grace_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._exporter = exporter
        self._max_size = max(1, max_size)
        self._batch_size = max(1, batch_size)
        self._interval_s = max(0.05, interval_s)
        self.name = name
        self._items: deque[tuple[ReadableSpan, str | None]] = deque()
        # a tracked span that never reached the exporter (the queue refused it, or the flush
        # budget ran out) against one the exporter reported as failed: the first is certainly
        # unwritten, the second may have been written and lost its answer (ADR-0048)
        self._unsent_tracked: set[str] = set()
        self._failed_tracked: set[str] = set()
        self._retrier = retrier
        self._grace_left = max(0.0, flush_grace_s) if retrier is not None else 0.0
        self._clock = clock
        self._sleep = sleep
        # the batch the exporter holds now; once closed, shutdown has counted it and a late
        # answer is not counted again
        self._in_flight: list[tuple[ReadableSpan, str | None]] = []
        self._closed = False
        self._flush_end: float | None = None
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

    def put(self, span: ReadableSpan, *, track: str | None = None) -> bool:
        with self._lock:
            if self._closed or len(self._items) >= self._max_size:
                self.dropped += 1
                if track is not None:
                    self._unsent_tracked.add(track)
                return False
            self._items.append((span, track))
            self.queued += 1
            ready = len(self._items) >= self._batch_size
        if ready:
            self._wake.set()
        return True

    def lost_tracked(self) -> set[str]:
        """Every track key whose span did not land: never sent, or sent and refused."""
        with self._lock:
            return self._unsent_tracked | self._failed_tracked

    def unsent_tracked(self) -> set[str]:
        """The track keys whose span never reached the exporter: certainly not written."""
        with self._lock:
            return set(self._unsent_tracked)

    def failed_tracked(self) -> set[str]:
        """The track keys the exporter reported as failed: the receiver may hold them anyway."""
        with self._lock:
            return set(self._failed_tracked)

    def _take_batch(self) -> list[tuple[ReadableSpan, str | None]]:
        with self._lock:
            if self._closed:
                return []
            count = min(self._batch_size, len(self._items))
            self._in_flight = [self._items.popleft() for _ in range(count)]
            return self._in_flight

    def _export(self, batch: list[tuple[ReadableSpan, str | None]]) -> None:
        spans = [span for span, _ in batch]
        try:
            result = self._exporter.export(spans)
        except Exception as exc:  # noqa: BLE001 - never propagate into the run
            _log.warning("span export raised: %s", exc)
            result = SpanExportResult.FAILURE
        with self._lock:
            self._in_flight = []
            if self._closed:
                return  # shutdown counted this batch as posted and unconfirmed already
            if result is SpanExportResult.SUCCESS:
                self.exported += len(batch)
            else:
                self.failures += 1
                self.dropped += len(batch)
                self._failed_tracked.update(track for _, track in batch if track is not None)

    def _drain(self, deadline: Callable[[], float] | None = None) -> None:
        """Export batches until the queue is empty or ``deadline()`` (``clock`` time, read before
        each batch) passes; the worker's batch in hand is awaited within it too."""
        if deadline is None:
            self._drain_lock.acquire()
        elif not self._acquire_before(deadline):
            return
        try:
            while deadline is None or self._clock() < deadline():
                batch = self._take_batch()
                if not batch:
                    return
                self._export(batch)
        finally:
            self._drain_lock.release()

    def _acquire_before(self, deadline: Callable[[], float]) -> bool:
        """The drain lock, unless the worker holds it past the deadline."""
        while not self._drain_lock.acquire(blocking=False):
            if self._clock() >= deadline():
                return False
            self._sleep(FLUSH_POLL_S)
        return True

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval_s)
            self._wake.clear()
            self._drain()
        self._drain()

    def flush(self, timeout_s: float | None = None) -> bool:
        """Export what is queued, in the caller's thread, within ``timeout_s`` (and the grace left,
        while the receiver refuses with an answer the retry policy waits out); whatever that did
        not cover is dropped and counted, so a receiver that is down cannot hold the run open.
        True when everything left the queue and no batch is still with the worker."""
        if timeout_s is None:
            self._drain()
            return self._everything_left()
        base = self._clock() + max(0.0, timeout_s)
        grace_end = base + self._grace_left
        retrier = self._retrier
        # a batch already waiting out a refusal when the flush starts counts as refused in it
        mark = retrier.refusal_mark() if retrier is not None else 0
        if retrier is not None:
            # a refused batch waits within the grace, never past it
            retrier.set_deadline(grace_end)

        def deadline() -> float:
            if retrier is not None and retrier.refusing_since(mark):
                return grace_end
            return base

        self._drain(deadline)
        self._grace_left = max(0.0, self._grace_left - max(0.0, self._clock() - base))
        self._flush_end = deadline()
        return self._everything_left()

    def _everything_left(self) -> bool:
        """Drop and count what is still queued; True when nothing was, and the worker holds no
        batch (a batch that finished after the deadline still left)."""
        left = self._drop_the_rest()
        with self._lock:
            return left == 0 and not self._in_flight

    def _drop_the_rest(self) -> int:
        """Count what is still queued as dropped (never sent); returns how many."""
        with self._lock:
            left = len(self._items)
            if left:
                self._unsent_tracked.update(track for _, track in self._items if track is not None)
                self._items.clear()
                self.dropped += left
        return left

    def shutdown(self, timeout_s: float = 30.0) -> bool:
        """Flush within the budget, stop the worker, close the exporter; True when nothing was lost.

        A batch the worker still holds once the budget is spent counts as posted and not
        confirmed, so the receipt accounts for every span the queue took."""
        flushed = self.flush(timeout_s)
        self._stop.set()
        self._wake.set()
        if self._retrier is not None:
            self._retrier.cancel()  # the run is over: no refused batch waits any longer
        end = self._flush_end if self._flush_end is not None else self._clock()
        self._thread.join(timeout=max(0.1, end - self._clock()))
        alive = self._thread.is_alive()
        with self._lock:
            if alive and self._in_flight:
                self.failures += 1
                self.dropped += len(self._in_flight)
                self._failed_tracked.update(t for _, t in self._in_flight if t is not None)
            self._closed = True
        try:
            self._exporter.shutdown()
        except Exception as exc:  # noqa: BLE001
            _log.warning("span exporter shutdown raised: %s", exc)
        return flushed and not alive

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
    """The post-trial step a receiver provides (``media.LangfuseAttachments``): the
    file attachments, the ingestion route the trial-end events take, inline media, and the
    budget and breaker both share. ``attach_with_manifest``, ``register_media``, ``budget``,
    ``note_trial_outcome`` and ``tripped`` are optional capabilities looked up with ``getattr``
    (a receiver with the bare ``attach`` / ``ingest`` pair still works). The observer scans a
    pass's events with its own gate, so the step needs no scanner of its own."""

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
    """What a trial's end leaves for its ``trial_persisted`` pass, after the state is dropped.

    ``identity`` and ``error`` are also what an error root is written from when the final root
    can no longer come."""

    started_at: datetime | None = None
    status: str | None = None
    judge_model: str | None = None
    user_model: str | None = None
    tags: tuple[str, ...] = ()
    # the trace's user (the agent's model identity), which an error root carries too
    user_id: str | None = None
    identity: TrialIdentity | None = None
    error: str | None = None
    root_sent: bool = False
    root_refused: bool = False
    finished_at: datetime | None = None


@dataclass
class _TrialState:
    identity: TrialIdentity
    started_at: datetime
    models: dict[str, ModelRef] = field(default_factory=dict)
    agent: ModelIdentity | None = None
    # whether the deployment's rules read the agent's model; a raw identity stands in for its tags
    # when they cannot, and names no user (the bundle pass has no identity for it either)
    agent_resolved: bool = False
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
        profile_version: str | None = None,
        projection: ProjectionSettings | None = None,
        server_api: str = SERVER_V3,
        previews: bool = False,
        gate: SafetyGate | None = None,
        ambient: Sequence[str] = (),
        retry_stats: RetryStats | None = None,
    ) -> None:
        self._queue = queue
        self._attachments = attachments
        # what the retry policy did on every write route of the run (the plugin shares one)
        self._retry_stats = retry_stats if retry_stats is not None else RetryStats()
        self._gradings = gradings
        self._projection = projection or ProjectionSettings()
        # the receiver family this run writes for: on v4 every observation is written
        # once and the record comes from the bundle
        self._server_api = server_api
        self._write_once = server_api == SERVER_V4
        # on v4 the live rows go out only as declared previews, and only when asked for: a
        # preview stays beside its final row, and the receiver counts every row it holds
        self._previews = self._write_once and previews
        self._write_once_counts = {
            "previews": 0,
            "error_roots": 0,
            "final_observations": 0,
            "roots_unconfirmed": 0,
        }
        # the trials whose final root has not been written yet, by trace id: one of them gets an
        # error root at run end unless its bundle pass writes the real one
        self._roots_pending: dict[str, _PersistContext] = {}
        self._grading_counts = {
            "sent": 0,
            "failed": 0,
            "withheld": 0,
            "scores": 0,
            "users": 0,
        }
        self._projection_counts = {
            "sent": 0,
            "failed": 0,
            "withheld": 0,
            "observations": 0,
            "events": 0,
            "media_uploaded": 0,
            "media_failed": 0,
        }
        self._expect_project = expect_project
        self._project_verified = project_verified
        self._profile_version = profile_version
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
        # one gate for the run: its spans, its trial-end passes and (when the plugin wires it so)
        # its file attachments scan with it
        self._gate = gate if gate is not None else SafetyGate.from_environment()
        self._spans_withheld = 0
        # what withheld a span or a pass, by rule and variable name, for the run-end summary
        self._withheld_by: Counter[str] = Counter()
        self._states: dict[str, _TrialState] = {}
        self._states_lock = threading.Lock()
        # the strings every span of the run carries by design: a known value one of them
        # contains would withhold every span, so the gate leaves it out (and says so)
        self._ambient = self._ambient_strings(ambient)
        self._left_out: set[str] = set()
        self._calibrate_gate()

    # -- TrialObserver ---------------------------------------------------------------------------

    def trial_started(
        self, identity: TrialIdentity, *, models: Mapping[str, ModelRef], started_at: datetime
    ) -> None:
        self._refresh_gate()
        agent_ref = models.get("agent")
        agent, agent_resolved = (
            self._resolve_read(agent_ref) if agent_ref is not None else (None, False)
        )
        tags: list[str] = [HARNESS_TAG, SOURCE_TAG]
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
            agent_resolved=agent_resolved,
            tags=tuple(tags),
        )
        with self._states_lock:
            self._states[identity.trace_id] = state
            if self._write_once:
                self._roots_pending[identity.trace_id] = _PersistContext(
                    started_at=started_at,
                    identity=identity,
                    tags=state.tags,
                    user_id=self._user_id(state),
                )
        if self._write_once:
            if self._previews:
                # (C) in shape R: the preview root names the final root as its parent, so the
                # trace has no root row until the bundle's root arrives and exactly one
                # afterwards. The trial is reachable meanwhile by its trace id and by session.
                self._emit(
                    name=f"{PREVIEW_NAME_PREFIX}{NAME_TRIAL}",
                    identity=identity,
                    span_id=self._preview_root_id(identity),
                    parent_id=identity.root_id,
                    attributes={
                        **self._trace_attributes(state),
                        **self._identity_attributes(identity),
                        "langfuse.observation.type": "agent",
                        f"{OBSERVATION_METADATA_PREFIX}kind": "root",
                        f"{OBSERVATION_METADATA_PREFIX}status": "running",
                        f"{OBSERVATION_METADATA_PREFIX}trace_time_source": TRACE_TIME_SOURCE,
                        **self._preview_marker(),
                    },
                    start=started_at,
                    end=started_at,
                    preview=True,
                )
            return
        # The root span goes out now, open-ended (end = start) and marked running, and again at
        # the end with everything it knows: the receiver dates the trace from the first span it
        # sees, which would otherwise be the first generation, seconds after the trial started.
        attributes = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "agent",
            "langfuse.trace.metadata.task_id": identity.task_id,
            "langfuse.trace.metadata.trial_index": identity.trial_index,
            "langfuse.trace.metadata.attempt": identity.attempt_id,
            "langfuse.trace.metadata.run_id": identity.run_id,
            "langfuse.trace.metadata.run_tag": identity.run_tag,
            "langfuse.trace.metadata.status": "running",
            "langfuse.trace.metadata.trace_time_source": TRACE_TIME_SOURCE,
        }
        self._emit(
            name=NAME_TRIAL,
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
        if self._nothing_live():
            return
        # a non-agent generation is a judge turn of the run's own grading (contract v2: kind
        # ``jgen`` under the grading id ``live:<run_id>``)
        agent_role = role == "agent"
        name = NAME_AGENT if agent_role else NAME_JUDGE
        usage = getattr(result, "usage", None)
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
        attributes.update(self._usage_cost_attributes(usage, result))
        kind, key = (
            ("gen", (index,)) if agent_role else ("jgen", (f"live:{identity.run_id}", index))
        )
        self._emit(
            name=self._live_name(name),
            identity=identity,
            span_id=self._live_observation_id(identity, kind, *key),
            parent_id=self._live_parent_id(identity),
            attributes={**attributes, **self._live_extras(identity)},
            start=started_at,
            end=ended_at,
            preview=self._write_once,
        )

    def _usage_cost_attributes(self, usage: Any, result: Any) -> dict[str, Any]:
        """A live generation's usage and cost attributes and its ``cost_basis``: the final
        figures on a receiver that updates rows in place, zeros (the figures as metadata) on a
        v4 preview, which stays in the trace beside the final row."""
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        details: dict[str, int] = {
            # the prompt total holds the cache reads and the cache writes: each leaves it once
            "input": max(0, prompt - cache_read - cache_creation),
            "output": completion,
            "total": prompt + completion,
        }
        if cache_read:
            details["cache_read_input_tokens"] = cache_read
        if cache_creation:
            details["cache_creation_input_tokens"] = cache_creation
        attributes: dict[str, Any] = {}
        # the call's own record carries the charge the provider stated; a result without one
        # (no usage block) has only the eval's figure, if any
        calls = getattr(usage, "calls", None) or ()
        cost, basis = call_cost(
            call_record(calls[-1]) if calls else {"cost_usd": getattr(result, "cost_usd", None)}
        )
        attributes["langfuse.observation.metadata.cost_basis"] = basis
        if self._write_once:
            # A preview row stays in the trace beside the final row the bundle writes, and the
            # receiver adds up the usage and cost of every row: a preview states zero usage and
            # cost (explicitly, so the receiver infers none from its model) and its figures as
            # metadata, so each call counts once, on its final row.
            attributes["langfuse.observation.usage_details"] = self._json(
                {"input": 0, "output": 0, "total": 0}
            )
            attributes["langfuse.observation.cost_details"] = self._json({"total": 0})
            attributes["langfuse.observation.metadata.prompt_tokens"] = prompt
            attributes["langfuse.observation.metadata.completion_tokens"] = completion
            attributes["langfuse.observation.metadata.cost"] = _attribute_value(cost)
        else:
            # on a receiver that updates rows in place the live row is the final row; a call
            # without any figure states an explicit zero, so the receiver prices nothing itself
            attributes["langfuse.observation.usage_details"] = self._json(details)
            attributes["gen_ai.usage.input_tokens"] = prompt
            attributes["gen_ai.usage.output_tokens"] = completion
            attributes["langfuse.observation.cost_details"] = self._json(
                {"total": cost if cost is not None else 0}
            )
        return attributes

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
        if self._nothing_live():
            return
        tool_name = str(getattr(call, "name", "tool"))
        # contract v2: a tool execution is keyed by the episode-unique call id the loop assigned
        # (the same value the bundle's tool_log.yaml and tool message carry), never by position
        key = _ids.tool_key(getattr(call, "id", None), index)
        span_id = (
            self._live_observation_id(identity, "tool", key)
            if role == "agent"
            else self._live_observation_id(identity, "jtool", f"live:{identity.run_id}", key)
        )
        name = f"tool: {tool_name}" if role == "agent" else f"judge tool: {tool_name}"
        success = bool(getattr(result, "success", True))
        output = (
            getattr(result, "output", "") if success else f"Error: {getattr(result, 'error', '')}"
        )
        attributes: dict[str, Any] = {
            **self._trace_attributes(state),
            "langfuse.observation.type": "tool",
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
            name=self._live_name(name),
            identity=identity,
            span_id=span_id,
            parent_id=self._live_parent_id(identity),
            attributes={**attributes, **self._live_extras(identity)},
            start=started_at,
            end=ended_at,
            error=not success,
            preview=self._write_once,
        )

    def trial_finished(
        self, identity: TrialIdentity, *, trajectory: Any, error: str | None = None
    ) -> None:
        """Close the trace with the root span, or, in the write-once layout, only keep what the
        bundle pass and a possible error root need: there the root is written from the bundle at
        ``trial_persisted``. ``trajectory`` is ``None`` when the trial died before producing one;
        ``error`` names the exception that ended it, if any."""
        with self._states_lock:
            state = self._states.pop(identity.trace_id, None)
        if state is None:
            state = _TrialState(
                identity=identity,
                started_at=_as_utc(getattr(trajectory, "start_ts", None))
                or datetime.now(tz=timezone.utc),
            )
        status = getattr(trajectory, "status", None)
        status_value = _enum_value(status) if trajectory is not None else "error"
        start = state.started_at or _as_utc(getattr(trajectory, "start_ts", None))
        context = _PersistContext(
            started_at=start,
            status=status_value,
            judge_model=self._model_name_of(state, "judge"),
            user_model=self._model_name_of(state, "user"),
            tags=state.tags,
            user_id=self._user_id(state),
            identity=identity,
            error=error,
            finished_at=_as_utc(getattr(trajectory, "end_ts", None))
            or datetime.now(tz=timezone.utc),
        )
        with self._states_lock:
            self._persist[identity.trace_id] = context
            if self._write_once:
                self._roots_pending[identity.trace_id] = context
        if self._write_once:
            # the root is one of the bundle's observations and is written once, last, at
            # trial_persisted; nothing about this trial may be written twice
            return
        grade = getattr(trajectory, "grade", None)
        metrics = getattr(trajectory, "metrics", None)
        usage = getattr(metrics, "usage", None)
        messages = list(getattr(trajectory, "messages", []) or [])
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
            "tool_calls": int(getattr(metrics, "tool_calls", 0) or 0),
            "tokens_input": int(getattr(usage, "prompt_tokens", 0) or 0),
            "tokens_output": int(getattr(usage, "completion_tokens", 0) or 0),
            "cost_usd": getattr(metrics, "cost_usd", None) if metrics is not None else None,
            "generations_observed": state.generations,
            "tool_calls_observed": state.tool_calls,
        }
        # the slim schema of the trial-end pass (the model facets are tags, the rest of the
        # trial is in the attached files); the pass overwrites these keys from the bundle
        metadata["model_name"] = state.agent.canonical if state.agent else NONE
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
            "langfuse.observation.type": "agent",
            "langfuse.trace.input": self._cap(_content(first_user)),
            "langfuse.trace.output": self._cap(_content(last_assistant)),
        }
        for key, value in metadata.items():
            attributes[f"langfuse.trace.metadata.{key}"] = _attribute_value(value)
        end = _as_utc(getattr(trajectory, "end_ts", None)) or datetime.now(tz=timezone.utc)
        self._emit(
            name=NAME_TRIAL,
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
        tags: list[str] = [HARNESS_TAG, SOURCE_TAG, f"task:{identity.task_id}"]
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
        """The persisted bundle's default projection (``projection``): the same
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
            metadata=dict(self._metadata),
            environment=settings.environment,
            release=settings.release,
            version=settings.version,
            producer=settings.producer,
            attach_mode=str(getattr(step, "mode", "all")),
            grades=self._gradings,
            derived_groups=settings.derived_groups,
            trace_name=settings.trace_name,
            trace_user=settings.trace_user,
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
        verdict = self._withheld_by_the_gate(projection.events, identity.trace_id, "projection")
        if verdict is not None:
            with self._states_lock:
                self._projection_counts[verdict] += 1
            return
        note = getattr(step, "note_trial_outcome", None)
        scores_sent = True
        if self._write_once:
            written, scores_sent, scores_tried = self._write_projection_once(
                identity, projection, manifest
            )
            if callable(note) and getattr(step, "mode", "all") == "none" and scores_tried:
                # the only call in this pass that waits for the receiver
                note(reached=scores_sent)
            if not written:
                if callable(note) and getattr(step, "mode", "all") == "none" and not scores_tried:
                    note(reached=False)
                return
        else:
            try:
                step.ingest(projection.events)
            except Exception as exc:  # noqa: BLE001 - the observability layer only warns
                reason = (
                    str(exc) if type(exc).__name__ == "LangfuseApiError" else type(exc).__name__
                )
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
                # a grading whose scores did not reach the receiver is not a grading that landed
                self._grading_counts["sent" if scores_sent else "failed"] += 1
            if scores_sent:
                self._grading_counts["scores"] += stats.scores
            self._grading_counts["users"] += stats.user_generations

    def _withheld_by_the_gate(
        self, events: list[dict[str, Any]], trace_id: str, what: str
    ) -> str | None:
        """The outbound data-safety gate over a pass's events, with the run's own gate whatever
        the attachment step offers: ``None`` when they may be sent, else the counter that takes
        the pass, ``withheld`` (they would carry a secret) or ``failed`` (they could not be
        scanned). Nothing is rewritten; the warning names the rules and the variables, never a
        value."""
        try:
            findings = self._gate.scan_structured(events)
        except Exception as exc:  # noqa: BLE001 - an unserialisable body is a failed pass
            _log.warning("%s: trace %s not scanned: %s", what, trace_id, type(exc).__name__)
            return "failed"
        if not findings:
            return None
        causes = _causes(findings)
        with self._states_lock:
            self._withheld_by.update(causes)
        _log.warning(
            "%s: trace %s not sent, the events would carry a secret: %s",
            what,
            trace_id,
            ", ".join(causes),
        )
        return "withheld"

    def _write_projection_once(
        self,
        identity: TrialIdentity,
        projection: Any,
        manifest: Mapping[str, Any] | None,
    ) -> tuple[bool, bool, bool]:
        """The bundle's records in the write-once layout: every observation as a span under its
        contract id, **the root last** (it completes the trace, so nothing may follow it), then
        the scores through the ingestion route, which still accepts them. Each id leaves exactly
        once; a failed score batch is counted and leaves the trace complete but unscored.
        Returns ``(anything was queued, the scores reached the receiver, the scores were tried
        at all)``. Only the score call is evidence about the receiver: queueing a span says
        nothing, because the queue takes it whether or not the endpoint answers, so the third
        value tells the caller whether there is anything to feed the breaker with."""
        settings = self._projection
        spans = spans_from_events(
            projection.events,
            environment=settings.environment,
            release=settings.release,
            version=settings.version,
            resource=self._resource,
            scope=self._scope,
        )
        root_queued = False
        queued_spans = 0
        for span in spans:
            is_root = format(span.context.span_id, "016x") == identity.root_id
            queued = self._queue.put(span, track=identity.trace_id if is_root else None)
            queued_spans += int(queued)
            if is_root:
                root_queued = queued
        with self._states_lock:
            self._write_once_counts["final_observations"] += queued_spans
            context = self._roots_pending.get(identity.trace_id)
            if context is not None and root_queued:
                context.root_sent = True
            if root_queued and manifest is not None:
                # on this receiver the manifest is part of the root's metadata; the attachment
                # step sent nothing, so the trace's manifest is counted here
                self._attach_counts.manifests_sent += 1
            elif manifest is not None:
                self._attach_counts.manifests_failed += 1
        if not root_queued:
            with self._states_lock:
                if context is not None:
                    context.root_refused = True
            _log.warning(
                "projection: the root observation of trace %s was not queued", identity.trace_id
            )
        scores = score_events(projection.events)
        scores_sent = True
        if scores:
            try:
                self._attachments.ingest(scores)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - the observability layer only warns
                reason = (
                    str(exc) if type(exc).__name__ == "LangfuseApiError" else type(exc).__name__
                )
                _log.warning(
                    "projection: scores of trace %s not sent: %s", identity.trace_id, reason
                )
                scores_sent = False
        return bool(queued_spans), scores_sent, bool(scores)

    def _send_gradings(
        self,
        identity: TrialIdentity,
        trial_dir: Path,
        judge_name: str | None,
        user_name: str | None,
    ) -> None:
        """The bundle's grading, judge transcript, scores and simulated user turns as ingestion
        events under the shared id contract (``gradings``; the ``gradings`` projection
        mode). Never raises."""
        from tolokaforge_langfuse.gradings import build_grading_events

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
        verdict = self._withheld_by_the_gate(built.events, identity.trace_id, "gradings")
        if verdict is not None:
            with self._states_lock:
                self._grading_counts[verdict] += 1
            return
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
        decided: set[str] = set()
        if self._write_once:
            # the queue is drained first: a root that never reached the exporter is only known
            # afterwards, and an error root for it still has to get out
            self._queue.flush(self._flush_timeout_s)
            decided = self._write_error_roots()
        flushed = self._queue.shutdown(self._flush_timeout_s)
        if self._write_once:
            self._count_roots_left_unconfirmed(decided)
        self._warn_of_what_was_withheld()
        with self._states_lock:
            self._persist.clear()  # trials that were never announced
            self._roots_pending.clear()
        counts = self._attach_counts
        return ExportReceipt(
            **self._queue.receipt(flushed=flushed).model_dump(exclude={"extra", "details"}),
            extra={
                "langfuse.attachments_registered": counts.registered,
                "langfuse.attachments_uploaded": counts.uploaded,
                "langfuse.attachments_deduplicated": counts.deduplicated,
                "langfuse.attachments_skipped": counts.skipped,
                "langfuse.attachments_failed": counts.failed,
                "langfuse.manifests_sent": counts.manifests_sent,
                "langfuse.manifests_failed": counts.manifests_failed,
                "langfuse.gradings_sent": self._grading_counts["sent"],
                "langfuse.gradings_failed": self._grading_counts["failed"],
                "langfuse.gradings_refused_secret": self._grading_counts["withheld"],
                "langfuse.scores_sent": self._grading_counts["scores"],
                "langfuse.user_generations_sent": self._grading_counts["users"],
                "langfuse.projections_sent": self._projection_counts["sent"],
                "langfuse.projections_failed": self._projection_counts["failed"],
                "langfuse.projections_refused_secret": self._projection_counts["withheld"],
                "langfuse.observations_sent": self._projection_counts["observations"],
                "langfuse.events_sent": self._projection_counts["events"],
                "langfuse.media_uploaded": self._projection_counts["media_uploaded"],
                "langfuse.media_failed": self._projection_counts["media_failed"],
                "langfuse.previews_sent": self._write_once_counts["previews"],
                "langfuse.error_roots_sent": self._write_once_counts["error_roots"],
                # a root the exporter posted and could not confirm: no second root is written
                "langfuse.roots_unconfirmed": self._write_once_counts["roots_unconfirmed"],
                "langfuse.final_observations_sent": self._write_once_counts["final_observations"],
                # live spans the data-safety gate withheld (neither queued nor dropped); a
                # trial-end pass it withheld is counted as ``*_refused_secret``, not ``*_failed``
                "langfuse.spans_refused_secret": self._spans_withheld,
                # what the retry policy did, over the span export and the trial-end calls
                **{f"langfuse.{key}": value for key, value in self._retry_stats.counts().items()},
            },
            details=(
                {
                    "exporter": "langfuse",
                    "expect_project": self._expect_project,
                    "project_verified": self._project_verified,
                    "server_api": self._server_api,
                    "previews": "on" if self._previews else "off",
                    # the receiver's native environment and the deployment profile of the run
                    "environment": self._projection.environment,
                    "profile_version": self._profile_version,
                },
            ),
        )

    def _write_error_roots(self) -> set[str]:
        """One minimal root for every trace whose real root can no longer come: the trial died
        before its bundle was written, the bundle pass wrote no root, or the root span never
        reached the receiver. Written once, at run end, so every trace of the run is in the
        trace list and the broken ones say so. Carries no manifest and no verdict. Returns the
        traces it decided about (an error root, or a root counted as unconfirmed)."""
        unsent = self._queue.unsent_tracked()
        unconfirmed = self._queue.failed_tracked()
        with self._states_lock:
            pending = [
                (trace_id, context)
                for trace_id, context in self._roots_pending.items()
                if not context.root_sent or trace_id in unsent
            ]
            # a root the exporter posted and could not confirm is NOT written again: the receiver
            # may hold it, and a second root under the same id could never be removed (ADR-0048).
            # The run says so instead, and the offline uploader completes such a trace later.
            ambiguous = sorted(
                trace_id
                for trace_id, context in self._roots_pending.items()
                if context.root_sent and trace_id in unconfirmed and trace_id not in unsent
            )
            self._write_once_counts["roots_unconfirmed"] += len(ambiguous)
        for trace_id in ambiguous:
            _log.warning(
                "the root observation of trace %s was posted but not confirmed; no error root is "
                "written for it, because the receiver may hold it already",
                trace_id,
            )
        for trace_id, context in pending:
            identity = context.identity
            if identity is None:  # a trial that was never announced has nothing to write
                continue
            if context.root_refused or trace_id in unsent:
                reason = "the root observation never left the queue"
            elif context.status is None:
                reason = context.error or "the trial did not finish"
            else:
                reason = (
                    f"the trace was not completed from the bundle (trial status: {context.status})"
                )
                if context.error:
                    reason = f"{reason}: {context.error}"
            started = context.started_at or datetime.now(tz=timezone.utc)
            # the trace's duration is its root's duration on this receiver, so a trial that died
            # in the fifth minute of a four-hour run must not be dated as a four-hour trace
            ended = context.finished_at or started
            written = self._emit(
                name=NAME_TRIAL,
                identity=identity,
                span_id=identity.root_id,
                parent_id=None,
                attributes={
                    **self._native_attributes(identity.task_id, context.tags, context.user_id),
                    **self._identity_attributes(identity),
                    "langfuse.observation.type": "agent",
                    "langfuse.observation.status_message": self._cap(reason),
                    f"{TRACE_METADATA_PREFIX}status": "error",
                    f"{TRACE_METADATA_PREFIX}error": self._cap(reason),
                    f"{TRACE_METADATA_PREFIX}label": self._label,
                    f"{TRACE_METADATA_PREFIX}trace_time_source": TRACE_TIME_SOURCE,
                    f"{OBSERVATION_METADATA_PREFIX}kind": "root",
                    f"{OBSERVATION_METADATA_PREFIX}error_root": True,
                },
                start=started,
                end=ended,
                error=True,
            )
            if written:
                with self._states_lock:
                    self._write_once_counts["error_roots"] += 1
        return {trace_id for trace_id, _ in pending} | set(ambiguous)

    def _count_roots_left_unconfirmed(self, decided: set[str]) -> None:
        """A root whose batch was still with the exporter when the error roots were written (a
        refused batch waiting out its retry) and failed after: posted and not confirmed, counted
        and warned about like the roots counted then."""
        failed = self._queue.failed_tracked()
        with self._states_lock:
            late = sorted(
                trace_id
                for trace_id, context in self._roots_pending.items()
                if context.root_sent and trace_id in failed and trace_id not in decided
            )
            self._write_once_counts["roots_unconfirmed"] += len(late)
        for trace_id in late:
            _log.warning(
                "the root observation of trace %s was posted but not confirmed before the run "
                "ended; no error root is written for it, because the receiver may hold it already",
                trace_id,
            )

    # -- helpers ------------------------------------------------------------------------------------

    def _resolve(self, ref: ModelRef) -> ModelIdentity:
        return self._resolve_read(ref)[0]

    def _resolve_read(self, ref: ModelRef) -> tuple[ModelIdentity, bool]:
        """The model's identity, and whether the deployment's rules read it (the raw identity
        stands in when they cannot)."""
        try:
            return self._resolver.resolve(ref.provider, ref.name), True
        except ModelNameResolverError as exc:
            _log.warning("model name not resolved, raw identity used: %s", exc)
            return RawModelNameResolver().resolve(ref.provider, ref.name), False

    def _state(self, identity: TrialIdentity) -> _TrialState:
        with self._states_lock:
            state = self._states.get(identity.trace_id)
            if state is None:
                state = _TrialState(
                    identity=identity,
                    started_at=datetime.now(tz=timezone.utc),
                    tags=(HARNESS_TAG, SOURCE_TAG, *self._tags),
                )
                self._states[identity.trace_id] = state
                if self._write_once:
                    # a trial whose start was never announced still owes its trace a root
                    self._roots_pending.setdefault(
                        identity.trace_id,
                        _PersistContext(
                            started_at=state.started_at, identity=identity, tags=state.tags
                        ),
                    )
            return state

    def _user_id(self, state: _TrialState) -> str | None:
        """The trace's user under the profile's ``[trace] user``: the agent's model identity when
        the deployment's rules read it (the bundle pass's rule), else nobody."""
        identity = state.agent.canonical if state.agent and state.agent_resolved else None
        return trace_user(self._projection.trace_user, identity)

    def _model_name_of(self, state: _TrialState, role: str) -> str | None:
        ref = state.models.get(role)
        if ref is None:
            return None
        return self._resolve(ref).canonical

    def _trace_attributes(self, state: _TrialState) -> dict[str, Any]:
        return self._native_attributes(state.identity.task_id, state.tags, self._user_id(state))

    def _native_attributes(
        self, task_id: str, tags: Sequence[str], user_id: str | None = None
    ) -> dict[str, Any]:
        # the receiver's native fields ride on every span: Langfuse fixes a trace's environment
        # at the first write it sees (verified on the instance 2026-09-17), and on v4 they are
        # stored and filtered per observation, so every row carries them. The name and the user
        # follow the profile's ``[trace]``, as the projection's do
        attributes: dict[str, Any] = {
            "langfuse.trace.name": trace_name(
                self._projection.trace_name, (*tags, f"task:{task_id}"), label=self._label
            ),
            "langfuse.session.id": self._session_id,
            "langfuse.trace.tags": list(tags),
        }
        if user_id:
            attributes["langfuse.user.id"] = user_id
        settings = self._projection
        if settings.environment is not None:
            attributes["langfuse.environment"] = settings.environment
        if settings.release is not None:
            attributes["langfuse.release"] = settings.release
        if settings.version is not None:
            attributes["langfuse.version"] = settings.version
        return attributes

    def _identity_attributes(self, identity: TrialIdentity) -> dict[str, Any]:
        """The trace metadata keys every row carries, so a child can be found by them alone;
        the values are the projection's, so a preview and the final row agree."""
        values: dict[str, Any] = {
            "task_id": identity.task_id,
            "trial_index": identity.trial_index,
            "attempt": str(identity.attempt_id),
            "run_id": identity.run_id,
            "run_tag": identity.run_tag,
        }
        return {f"{TRACE_METADATA_PREFIX}{key}": values[key] for key in IDENTITY_METADATA_KEYS}

    # -- the live rows: final on a v3 receiver, declared previews on a write-once one -----------

    def _nothing_live(self) -> bool:
        """The write-once layout without previews: a trial is written once, when persisted."""
        return self._write_once and not self._previews

    def _preview_root_id(self, identity: TrialIdentity) -> str:
        return identity.observation_id(_ids.preview_kind("root"), _ids.ROOT_KEY)

    def _preview_marker(self) -> dict[str, Any]:
        return {f"{OBSERVATION_METADATA_PREFIX}{PREVIEW_METADATA_KEY}": True}

    def _live_name(self, name: str) -> str:
        return f"{PREVIEW_NAME_PREFIX}{name}" if self._write_once else name

    def _live_observation_id(self, identity: TrialIdentity, kind: str, *key: object) -> str:
        return identity.observation_id(_ids.preview_kind(kind) if self._write_once else kind, *key)

    def _live_parent_id(self, identity: TrialIdentity) -> str:
        return self._preview_root_id(identity) if self._write_once else identity.root_id

    def _live_extras(self, identity: TrialIdentity) -> dict[str, Any]:
        if not self._write_once:
            return {}
        return {**self._identity_attributes(identity), **self._preview_marker()}

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
        preview: bool = False,
    ) -> bool:
        """Queue one span; False when it was withheld (a secret) or the queue was full. Every live
        span of every kind leaves through here."""
        attrs = {k: v for k, v in attributes.items() if v is not None}
        if self._carries_secret(name, identity, span_id, attrs):
            return False
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
            attributes=attrs,
            events=(),
            links=(),
            kind=SpanKind.INTERNAL,
            instrumentation_scope=self._scope,
            status=Status(StatusCode.ERROR) if error else Status(StatusCode.UNSET),
            start_time=_nanos(start_dt),
            end_time=_nanos(end_dt),
        )
        queued = self._queue.put(span)
        if queued and preview:
            with self._states_lock:
                self._write_once_counts["previews"] += 1
        return queued

    def _carries_secret(
        self, name: str, identity: TrialIdentity, span_id: str, attributes: Mapping[str, Any]
    ) -> bool:
        """The data-safety gate over a span's name and attributes, before it is queued. A hit
        withholds the span, counts it, and warns with the span, the rules and the variables the
        known values came from, never a value."""
        findings = self._gate.scan_structured({"name": name, "attributes": attributes})
        if not findings:
            return False
        causes = _causes(findings)
        with self._states_lock:
            self._spans_withheld += 1
            self._withheld_by.update(causes)
        # a tool's name comes from the model: it is named only when it is clean itself
        shown = name if not self._gate.scan(name.encode("utf-8", "replace")) else "(name withheld)"
        _log.warning(
            "span %r (%s) of trace %s not exported, it would carry a secret: %s",
            shown,
            span_id,
            identity.trace_id,
            ", ".join(causes),
        )
        return True

    def _ambient_strings(self, extra: Sequence[str]) -> tuple[str, ...]:
        """What every span of the run carries by design: its session, label and tags, the
        caller's metadata, the receiver's native fields, and the run's id and tag (``extra``)."""
        projection = self._projection
        return tuple(
            text
            for text in (
                self._session_id,
                self._label,
                HARNESS_TAG,
                SOURCE_TAG,
                *self._tags,
                *strings_in(self._metadata),
                projection.environment,
                projection.release,
                projection.version,
                projection.producer,
                projection.trace_name,
                *extra,
            )
            if text
        )

    def _calibrate_gate(self) -> None:
        """Leave out of the gate every known value the run's own values contain: with it the
        gate would withhold every span."""
        self._say_left_out(self._gate.drop_ambient(self._ambient))

    def _refresh_gate(self) -> None:
        """Re-read the gate's known values when what they came from changed (a secret registered
        after the observer was built), leaving the ambient ones out again."""
        try:
            _, left_out = self._gate.refresh(self._ambient)
        except Exception as exc:  # noqa: BLE001 - the gate keeps the values it has
            _log.warning(
                "the data-safety gate's credentials were not re-read: %s", type(exc).__name__
            )
            return
        self._say_left_out(left_out)

    def _say_left_out(self, names: Sequence[str]) -> None:
        """Warn once per variable, by name, that its value is not scanned for."""
        for name in names:
            with self._states_lock:
                new = name not in self._left_out
                self._left_out.add(name)
            if new:
                _log.warning(
                    "%s holds a value the run's own tracing values (session, run, label, tags, "
                    "metadata, environment, release) contain, so the data-safety scan leaves it "
                    "out: every span would be withheld otherwise",
                    name,
                )

    def _warn_of_what_was_withheld(self) -> None:
        """One warning at run end when the gate withheld anything: the per-span lines say which,
        this one says that the run's traces are incomplete and why, by rule and variable."""
        passes = self._projection_counts["withheld"], self._grading_counts["withheld"]
        if not (self._spans_withheld or any(passes)):
            return
        _log.warning(
            "live tracing withheld %d span(s), %d projection pass(es) and %d gradings pass(es) "
            "that would have carried a secret (%s); the traces of this run are incomplete",
            self._spans_withheld,
            *passes,
            ", ".join(f"{cause} x{count}" for cause, count in self._withheld_by.most_common(5)),
        )

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


def _causes(findings: Sequence[Finding]) -> list[str]:
    """What the gate found, by rule and variable name, once each and never a value."""
    return list(dict.fromkeys(finding.describe() for finding in findings))


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
