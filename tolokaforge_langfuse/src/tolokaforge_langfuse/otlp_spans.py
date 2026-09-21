"""The projection's ingestion bodies as OTLP spans (ADR-0048, the v4 write-once layout).

A Langfuse v4 receiver in its default write mode takes observations over OTLP alone: the legacy
``trace-create`` / ``span-create`` / ``generation-create`` / ``event-create`` events are refused,
and **a trace is its root observation**. This module turns the ingestion bodies
:mod:`tolokaforge_langfuse.projection` builds (and the connector's ``mapping``
builds from the same bundle) into finished spans, so one body builder still serves both
producers and the v4 write path is a translation rather than a second projection.

What it does, and nothing else:

- the ``trace-create`` body becomes the trace's facts: name, session, tags, the native
  ``environment`` / ``release`` / ``version``, the trace metadata, and the input and output,
  which become the **root observation's** own (the trace is that observation);
- every observation body becomes one span under its own id, with its parent, clocks, level,
  status message, input, output, model, usage and cost, and its own metadata;
- the trace's name, session, tags, native fields and **identity** metadata keys ride on every
  span, because a v4 receiver stores and filters them per observation;
- the **root span is last**: a child may arrive before its root, and until the root lands
  the trace has no root row at all, so nothing can read a half-written trace as finished;
- ``score-create`` events are not spans. Scores keep the ingestion route on v4 and are returned
  untouched by :func:`score_events`.

Values follow the receiver's measured OTLP mapping: a nested metadata value travels as a JSON
string and is parsed back into an object, ``usage_details`` / ``cost_details`` JSON strings
become the typed maps, and an attribute cannot be null, so a metadata key whose value is ``None``
travels as the string ``none`` (the value the projection itself writes for an absent fact).

Engine-free by construction: it reads bodies, not trials, so the offline connector imports it
next to any engine pin, or with none.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

NONE = "none"
TRACE_EVENT = "trace-create"
SCORE_EVENT = "score-create"
# the ingestion event types that carry an observation, and the receiver's kind for each
OBSERVATION_EVENTS: Mapping[str, str] = {
    "span-create": "span",
    "generation-create": "generation",
    "event-create": "event",
}
# the trace metadata keys that ride on every span so a child row can be found by them alone
IDENTITY_METADATA_KEYS: tuple[str, ...] = (
    "task_id",
    "trial_index",
    "attempt",
    "run_id",
    "run_tag",
)
TRACE_METADATA_PREFIX = "langfuse.trace.metadata."
OBSERVATION_METADATA_PREFIX = "langfuse.observation.metadata."
DEFAULT_SERVICE_NAME = "tolokaforge"
DEFAULT_SCOPE_NAME = "tolokaforge.observability"


@dataclass(frozen=True)
class TraceFacts:
    """What the ``trace-create`` body says; applied to every span of the trace."""

    name: str | None = None
    session_id: str | None = None
    tags: tuple[str, ...] = ()
    environment: str | None = None
    release: str | None = None
    version: str | None = None
    input: Any = None
    output: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def identity_metadata(self) -> dict[str, Any]:
        return {k: self.metadata[k] for k in IDENTITY_METADATA_KEYS if k in self.metadata}


def trace_facts(
    events: Sequence[Mapping[str, Any]],
    *,
    environment: str | None = None,
    release: str | None = None,
    version: str | None = None,
) -> TraceFacts:
    """The trace's facts from the ``trace-create`` event; the arguments override its native
    fields (the deployment's values, which the caller resolves once per run)."""
    body: Mapping[str, Any] = {}
    for event in events:
        if event.get("type") == TRACE_EVENT:
            body = event.get("body") or {}
            break
    return TraceFacts(
        name=body.get("name"),
        session_id=body.get("sessionId"),
        tags=tuple(body.get("tags") or ()),
        environment=environment if environment is not None else body.get("environment"),
        release=release if release is not None else body.get("release"),
        version=version if version is not None else body.get("version"),
        input=body.get("input"),
        output=body.get("output"),
        metadata=dict(body.get("metadata") or {}),
    )


def score_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The ``score-create`` events, unchanged: scores keep the ingestion route on v4."""
    return [dict(event) for event in events if event.get("type") == SCORE_EVENT]


def observation_bodies(events: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """``(event type, body)`` of every observation, **the root last**."""
    children: list[tuple[str, dict[str, Any]]] = []
    roots: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        kind = str(event.get("type") or "")
        if kind not in OBSERVATION_EVENTS:
            continue
        body = dict(event.get("body") or {})
        (roots if not body.get("parentObservationId") else children).append((kind, body))
    return children + roots


def span_attributes(
    event_type: str, body: Mapping[str, Any], facts: TraceFacts, *, is_root: bool
) -> dict[str, Any]:
    """The Langfuse OTLP attributes of one observation body under ``facts``.

    A root carries the whole trace metadata and, as its own input and output, the trace's; a
    child carries the identity keys and its own body alone. Trace metadata wins over an
    observation metadata key of the same name: both land in one flat namespace on the receiver's
    side, so only one may be written.
    """
    attributes: dict[str, Any] = {
        "langfuse.observation.type": OBSERVATION_EVENTS[event_type],
    }
    _set(attributes, "langfuse.trace.name", facts.name)
    _set(attributes, "langfuse.session.id", facts.session_id)
    if facts.tags:
        attributes["langfuse.trace.tags"] = list(facts.tags)
    _set(attributes, "langfuse.environment", facts.environment)
    _set(attributes, "langfuse.release", facts.release)
    _set(attributes, "langfuse.version", facts.version)

    trace_metadata = dict(facts.metadata) if is_root else facts.identity_metadata
    for key, value in trace_metadata.items():
        attributes[f"{TRACE_METADATA_PREFIX}{key}"] = _metadata_value(value)
    for key, value in (body.get("metadata") or {}).items():
        if key in trace_metadata:
            continue  # the trace's fact wins; the receiver keeps one flat namespace
        attributes[f"{OBSERVATION_METADATA_PREFIX}{key}"] = _metadata_value(value)

    # The trace **is** its root observation here, and only the observation pair is stored: a
    # receiver consumes ``langfuse.trace.input`` / ``.output`` and keeps them nowhere (measured
    # on 4.38.0), so the trace's input and output are written as the root's own.
    payload_input = body.get("input")
    payload_output = body.get("output")
    if is_root:
        payload_input = facts.input if payload_input is None else payload_input
        payload_output = facts.output if payload_output is None else payload_output
    _set(attributes, "langfuse.observation.input", _payload(payload_input))
    _set(attributes, "langfuse.observation.output", _payload(payload_output))
    _set(attributes, "langfuse.observation.level", body.get("level"))
    _set(attributes, "langfuse.observation.status_message", body.get("statusMessage"))
    _set(attributes, "langfuse.observation.model.name", body.get("model"))
    for field_name, attribute in (
        ("usageDetails", "langfuse.observation.usage_details"),
        ("costDetails", "langfuse.observation.cost_details"),
    ):
        value = body.get(field_name)
        if value:
            attributes[attribute] = json.dumps(value, ensure_ascii=False, default=str)
    return attributes


def spans_from_events(
    events: Sequence[Mapping[str, Any]],
    *,
    environment: str | None = None,
    release: str | None = None,
    version: str | None = None,
    resource: Resource | None = None,
    scope: InstrumentationScope | None = None,
) -> list[ReadableSpan]:
    """Every observation of a projection as a finished span, the root last.

    ``environment`` / ``release`` / ``version`` are the deployment's native fields; when a value
    is ``None`` the trace body's own is used. Scores are not spans (:func:`score_events`).
    """
    facts = trace_facts(events, environment=environment, release=release, version=version)
    resource = resource or Resource.create({"service.name": DEFAULT_SERVICE_NAME})
    scope = scope or InstrumentationScope(DEFAULT_SCOPE_NAME, None)
    spans: list[ReadableSpan] = []
    for event_type, body in observation_bodies(events):
        is_root = not body.get("parentObservationId")
        attributes = span_attributes(event_type, body, facts, is_root=is_root)
        spans.append(
            _span(
                body,
                attributes,
                resource=resource,
                scope=scope,
                error=str(body.get("level") or "") == "ERROR",
            )
        )
    return spans


# -- helpers ------------------------------------------------------------------------------------


def _set(attributes: dict[str, Any], key: str, value: Any) -> None:
    """Write an attribute unless the value is absent: OTLP carries no null."""
    if value is None or value == "":
        return
    attributes[key] = value


def _metadata_value(value: Any) -> Any:
    """A metadata value as an OTLP attribute: scalars as they are, a nested value as a JSON
    string the receiver parses back, an absent value as the projection's own ``none``."""
    if value is None:
        return NONE
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _payload(value: Any) -> str | None:
    """An input or output field as text: a string as it is, anything else as JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _nanos(value: str | None, *, fallback: int | None = None) -> int | None:
    if not value:
        return fallback
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _span(
    body: Mapping[str, Any],
    attributes: Mapping[str, Any],
    *,
    resource: Resource,
    scope: InstrumentationScope,
    error: bool,
) -> ReadableSpan:
    trace_int = int(str(body["traceId"]), 16)
    context = SpanContext(
        trace_id=trace_int,
        span_id=int(str(body["id"]), 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    parent_id = body.get("parentObservationId")
    parent = (
        SpanContext(
            trace_id=trace_int,
            span_id=int(str(parent_id), 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        if parent_id
        else None
    )
    now = int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000)
    start = _nanos(body.get("startTime"), fallback=now)
    end = _nanos(body.get("endTime"), fallback=start)
    assert start is not None and end is not None  # the fallbacks above are never None
    return ReadableSpan(
        name=str(body.get("name") or ""),
        context=context,
        parent=parent,
        resource=resource,
        attributes=dict(attributes),
        events=(),
        links=(),
        kind=SpanKind.INTERNAL,
        instrumentation_scope=scope,
        status=Status(StatusCode.ERROR) if error else Status(StatusCode.UNSET),
        start_time=start,
        end_time=max(start, end),
    )
