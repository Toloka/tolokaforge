"""The default Langfuse projection of a persisted trial (ADR-0047, parity amendment).

Once a trial's bundle is on disk (``trial_persisted``) the live trace is completed from the
files, so that the trace the engine leaves behind is the same trace the offline bundle uploader
(``langfuse-connector`` in tolokaforge-tools, its ``mapping.py``) would produce from the same
bundle: the full trace metadata (every key with an explicit value, because the receiver merges
metadata and an omitted key persists), the root observation, the agent generations with their
paired usage, the tool executions from the grader's ``tool_log.yaml`` with the transcript text
beside them, the user simulator's own tool calls, the simulated user turns, the grading with its
judge transcript and scores, the trace-level mirror, the events of ``logs.yaml``, a limit hit, a
provisioning failure, service captures and reply-guard records, and media for base64 image
blocks. Everything travels through the receiver's ingestion API under the shared id contract
(``ids``), so the live spans are the preview and the bundle projection is the truth.

Vendor-neutral in what it knows: the module carries no deployment value. Tags, the environment,
the metadata keys mirrored from tags and the fixed metadata arrive through the deployment
profile (``profile``) and the launcher; the module applies them mechanically. The bundle is the
source of truth and nothing is invented for a field it does not carry. Nothing here raises for a
malformed bundle: a file that cannot be read yields what the rest of the bundle allows.

Shapes follow the connector's ``mapping.py`` and are guarded by a golden parity test that lives
in both repositories.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.observability import ids
from tolokaforge.observability.observer import TrialIdentity
from tolokaforge_langfuse.attachments import ATTACHMENTS_SCHEMA
from tolokaforge_langfuse.gradings import (
    CONTEXT_CHARS,
    CONTEXT_MESSAGES,
    NONE,
    OBSERVATION_KIND_GRADING,
    PROVENANCE_KEYS,
    UNKNOWN,
    _grading_input,
    _judge_observations,
    _normalize_ts,
    _number,
    _score_bodies,
    _text,
    content_fingerprint,
    grade_summary,
)
from tolokaforge_langfuse.model_names import (
    ModelIdentity,
    ModelNameResolver,
    ModelNameResolverError,
)

_log = logging.getLogger(__name__)

HARNESS = "tolokaforge"
UPLOAD_MODE_LIVE = "live"
TRACE_TIME_SOURCE_LIVE = "live"
USAGE_MATCH_GENERATION_ID = "generation_id"
USAGE_MATCH_POSITIONAL = "positional"
USAGE_MATCH_UNMATCHED = "unmatched"
OBSERVATION_KIND_ROOT = "root"
PROJECTION_FULL = "full"
PROJECTION_GRADINGS = "gradings"
PROJECTION_NONE = "none"
PROJECTION_MODES = (PROJECTION_FULL, PROJECTION_GRADINGS, PROJECTION_NONE)
# the metadata keys whose values differ by producer by design (the connector's exclusion list,
# PLAN 3.12): each producer writes them, a parity check compares them by name only
PRODUCER_KEYS = frozenset({"upload_mode", "uploader_version", "trace_time_source", "attach_mode"})
# what the live root span adds and the bundle cannot know, plus the two keys a Langfuse receiver
# writes into the metadata of a trace that arrived over OTLP (the trace-level span's raw
# attributes and the resource attributes); a caller may not use these names either
LIVE_ONLY_KEYS = frozenset(
    {
        "generations_observed",
        "tool_calls_observed",
        "error",
        "attributes",
        "resourceAttributes",
        "scope",
    }
)
# the part of the primary grading's summary a trace mirrors (the connector's list; the full
# summary stays on the grading observation and every detail is a score)
TRACE_GRADE_SUMMARY_KEYS: tuple[str, ...] = ("pass", "score", "judge_status")

# (trace_id, observation_id, field, content_type, raw bytes) -> media token, or None
MediaHandler = Callable[[str, str, str, str, bytes], "str | None"]


@dataclass(frozen=True)
class ProjectionContext:
    """What the run adds to a bundle: presentation, the deployment's values, the producer."""

    label: str
    session_id: str
    tags: tuple[str, ...]  # the trace's final tag list (producer and launcher tags)
    metadata: Mapping[str, Any] = field(default_factory=dict)  # caller + profile fixed metadata
    environment: str | None = None
    release: str | None = None
    version: str | None = None
    producer: str = HARNESS  # the ``uploader_version`` value: this producer's identity
    attach_mode: str = "all"
    grades: bool = True  # send the run's own grading (grade.yaml) with its scores


@dataclass
class ProjectionStats:
    observations: int = 0
    scores: int = 0
    events: int = 0
    media_uploaded: int = 0
    media_failed: int = 0
    usage_match: str = USAGE_MATCH_UNMATCHED
    grading_id: str | None = None
    user_generations: int = 0
    judge_observations: int = 0


@dataclass
class Projection:
    """The ingestion events of one persisted trial and what they hold."""

    trace_id: str
    events: list[dict[str, Any]]
    trace_body: dict[str, Any]
    stats: ProjectionStats


@dataclass(frozen=True)
class Bundle:
    """The parsed documents of one trial directory plus the run-level files next to it."""

    trajectory: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    grade: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    judge_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    logs: dict[str, Any] = field(default_factory=dict)
    limit_hit: dict[str, Any] = field(default_factory=dict)
    captures: dict[str, Any] = field(default_factory=dict)
    models_fingerprint: dict[str, Any] = field(default_factory=dict)
    env_sha256: str | None = None


# -- reading the bundle -------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        _log.warning("projection: %s not read: %s", path.name, type(exc).__name__)
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning("projection: %s not read: %s", path.name, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def load_bundle(trial_dir: Path) -> Bundle:
    """Every document the projection reads; a missing or unreadable file loads as empty."""
    trajectory = _mapping(_read_yaml(trial_dir / "trajectory.yaml"))
    judge_doc = _read_yaml(trial_dir / "judge_trajectory.yaml")
    judge_messages: list[dict[str, Any]] = []
    if isinstance(judge_doc, Mapping) and isinstance(judge_doc.get("messages"), list):
        judge_messages = [dict(m) for m in judge_doc["messages"] if isinstance(m, Mapping)]
    elif isinstance(judge_doc, list):
        judge_messages = [dict(m) for m in judge_doc if isinstance(m, Mapping)]
    grade = _mapping(_read_yaml(trial_dir / "grade.yaml"))
    if not judge_messages and isinstance(grade.get("judge_transcript"), list):
        judge_messages = [dict(m) for m in grade["judge_transcript"] if isinstance(m, Mapping)]
    tool_log = _read_yaml(trial_dir / "tool_log.yaml")
    run_dir = trial_dir.parent.parent.parent if trial_dir.parent.parent.name == "trials" else None
    engine_state = _read_json(run_dir / "engine_run_state.json") if run_dir else {}
    env_path = trial_dir / "env.yaml"
    env_raw = env_path.read_bytes() if env_path.exists() else None
    return Bundle(
        trajectory=trajectory,
        metrics=_mapping(_read_yaml(trial_dir / "metrics.yaml")),
        grade=grade,
        task=_mapping(_read_yaml(trial_dir / "task.yaml")),
        judge_messages=judge_messages,
        tool_log=(
            [dict(e) for e in tool_log if isinstance(e, Mapping)]
            if isinstance(tool_log, list)
            else []
        ),
        logs=_mapping(_read_yaml(trial_dir / "logs.yaml")),
        limit_hit=_read_json(run_dir / "LIMIT_HIT.json") if run_dir else {},
        captures=_mapping(_read_yaml(trial_dir / "services" / "_capture.yaml")),
        models_fingerprint=_mapping(engine_state.get("models_fingerprint")),
        env_sha256=hashlib.sha256(env_raw).hexdigest() if env_raw is not None else None,
    )


# -- models -----------------------------------------------------------------------------------------


def _pair(spec: object) -> tuple[str | None, str | None]:
    """(name, provider) of a ``{name, provider}`` block; anything else is unrecorded."""
    if not isinstance(spec, Mapping):
        return None, None
    name, provider = spec.get("name"), spec.get("provider")
    return (str(name) if name not in (None, "") else None), (
        str(provider) if provider not in (None, "") else None
    )


def agent_model(task: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(name, provider) of the agent model ``task.yaml`` records; a harness bundle names the
    harness as a string and the model next to it under ``model_info``."""
    config = _mapping(task.get("model_config"))
    if isinstance(config.get("agent"), str):
        return _pair(config.get("model_info"))
    return _pair(config.get("agent"))


def role_model(task: Mapping[str, Any], role: str) -> tuple[str | None, str | None]:
    config = _mapping(task.get("model_config"))
    return _pair(config.get(role))


def _resolve(
    resolver: ModelNameResolver, provider: str | None, name: str | None
) -> ModelIdentity | None:
    if not name:
        return None
    try:
        return resolver.resolve(provider, name)
    except ModelNameResolverError as exc:
        _log.warning("projection: model reference (%r, %r) not resolved: %s", provider, name, exc)
        return None


def _role_canonical(task: Mapping[str, Any], role: str, resolver: ModelNameResolver) -> str | None:
    name, provider = role_model(task, role)
    if not name:
        return None
    try:
        return resolver.resolve(provider, name).canonical
    except ModelNameResolverError:
        return "unreadable"


# -- clocks, usage, media ---------------------------------------------------------------------------


def trace_time(trajectory: Mapping[str, Any]) -> tuple[str | None, str]:
    """(timestamp, source) of the trace: ``start_ts``, else the first message ``ts``, else
    ``end_ts``; None only for a bundle without any clock."""
    start = _normalize_ts(trajectory.get("start_ts"))
    if start:
        return start, "start_ts"
    for message in trajectory.get("messages") or []:
        ts = _normalize_ts(message.get("ts")) if isinstance(message, Mapping) else None
        if ts:
            return ts, "message_ts"
    end = _normalize_ts(trajectory.get("end_ts"))
    if end:
        return end, "end_ts"
    return None, "upload_time"


def pair_usage(
    messages: Sequence[Mapping[str, Any]], calls: Sequence[Mapping[str, Any]]
) -> tuple[str, dict[int, Mapping[str, Any]]]:
    """assistant message index -> usage call: by ``openrouter_generation_id`` when every
    assistant message names one a call carries (ids unique), positionally only when the counts
    are equal, else unmatched (the trace alone carries totals)."""
    assistant = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    by_generation: dict[str, Mapping[str, Any]] = {}
    duplicated = False
    for call in calls:
        generation = call.get("openrouter_generation_id")
        if generation:
            duplicated |= generation in by_generation
            by_generation[generation] = call
    message_ids = [messages[i].get("openrouter_generation_id") for i in assistant]
    if (
        assistant
        and by_generation
        and not duplicated
        and len(set(message_ids)) == len(message_ids)
        and all(generation in by_generation for generation in message_ids)
    ):
        return USAGE_MATCH_GENERATION_ID, {
            i: by_generation[messages[i]["openrouter_generation_id"]] for i in assistant
        }
    if assistant and calls and len(assistant) == len(calls):
        return USAGE_MATCH_POSITIONAL, dict(zip(assistant, calls))
    return USAGE_MATCH_UNMATCHED, {}


def usage_fields(call: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    """(usageDetails, generation metadata): a non-overlapping breakdown with an explicit total."""
    prompt = int(call.get("prompt_tokens") or 0)
    completion = int(call.get("completion_tokens") or 0)
    cache_read = call.get("cache_read_input_tokens")
    cache_creation = call.get("cache_creation_input_tokens")
    uncached_input = prompt - int(cache_read or 0)
    details: dict[str, int] = {
        "input": max(uncached_input, 0),
        "output": completion,
        "total": prompt + completion,
    }
    if cache_read is not None:
        details["cache_read_input_tokens"] = int(cache_read)
    if cache_creation is not None:
        details["cache_creation_input_tokens"] = int(cache_creation)
    metadata = {
        "cached_tokens": call.get("cached_tokens"),
        "reasoning_tokens": call.get("reasoning_tokens"),
        "latency_s": call.get("latency_s"),
        "cost_source": call.get("cost_source"),
        "openrouter_generation_id": call.get("openrouter_generation_id"),
        "usage_clamped": uncached_input < 0,
    }
    return details, metadata


def _replace_media(
    obj: Any,
    trace_id: str,
    observation_id: str,
    field_name: str,
    media: MediaHandler | None,
    stats: ProjectionStats,
) -> Any:
    """Base64 image blocks become media tokens (or a small placeholder): raw base64 never
    enters an ingestion body."""
    if isinstance(obj, Mapping):
        source = obj.get("source")
        if (
            obj.get("type") == "image"
            and isinstance(source, Mapping)
            and source.get("type") == "base64"
            and source.get("data")
        ):
            content_type = source.get("media_type", "image/png")
            if media is not None:
                try:
                    raw = base64.b64decode(source["data"])
                    token = media(trace_id, observation_id, field_name, content_type, raw)
                    if token:
                        stats.media_uploaded += 1
                        return token
                except Exception as exc:  # noqa: BLE001 - the block becomes a placeholder
                    stats.media_failed += 1
                    return {
                        "type": "image",
                        "note": f"media upload failed: {type(exc).__name__}",
                        "media_type": content_type,
                    }
            return {"type": "image", "note": "media stripped", "media_type": content_type}
        return {
            key: _replace_media(value, trace_id, observation_id, field_name, media, stats)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [
            _replace_media(item, trace_id, observation_id, field_name, media, stats) for item in obj
        ]
    return obj


def envelope(event_type: str, body: dict[str, Any]) -> dict[str, Any]:
    """One ingestion event; the envelope id is per send (an update needs a new one)."""
    return {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "body": body,
    }


# -- observations -----------------------------------------------------------------------------------


def root_observation(
    trace_id: str,
    root_id: str,
    trajectory: Mapping[str, Any],
    *,
    task_id: str,
    trial_index: object,
) -> dict[str, Any]:
    start, _ = trace_time(trajectory)
    end = _normalize_ts(trajectory.get("end_ts")) or start
    status = _text(trajectory.get("status"))
    return {
        "id": root_id,
        "traceId": trace_id,
        "name": f"trial {task_id}/{trial_index}",
        "startTime": start,
        "endTime": end,
        "level": "ERROR" if status in ("error", "failed", "timeout") else "DEFAULT",
        "statusMessage": _text(trajectory.get("termination_reason")),
        "metadata": {
            "kind": OBSERVATION_KIND_ROOT,
            "task_id": task_id,
            "trial_index": trial_index,
            "status": status,
            "termination_reason": _text(trajectory.get("termination_reason")),
            "message_count": len(trajectory.get("messages") or []),
        },
    }


def _end_from_latency(started: str | None, latency: object) -> str | None:
    if not started or latency in (None, "") or not isinstance(latency, (int, float, str)):
        return None
    try:
        base = datetime.fromisoformat(started.replace("Z", "+00:00"))
        ended: str = (base + timedelta(seconds=float(latency))).isoformat()
        return ended.replace("+00:00", "Z") if started.endswith("Z") else ended
    except (TypeError, ValueError):
        return None


def _tool_body_from_log(
    trace_id: str,
    root_id: str,
    entry: Mapping[str, Any],
    *,
    transcript: Mapping[str, Any] | None,
    message_index: int | None,
    fallback_start: str | None,
) -> dict[str, Any]:
    """One tool execution as the grader recorded it (status, executor, latency, sequence, the
    untruncated output); the transcript's agent-facing text rides beside it when it differs."""
    call_id = str(entry["call_id"])
    started = _normalize_ts(entry.get("timestamp")) or fallback_start
    ended = _end_from_latency(started, entry.get("latency_seconds")) or started
    status = _text(entry.get("status"))
    success = status in ("success", "ok", NONE)
    output = entry.get("output")
    transcript_text = transcript.get("content") if transcript else None
    metadata: dict[str, Any] = {
        "role": "agent_tool" if _text(entry.get("executor")) != "user" else "user_tool",
        "kind": "tool",
        "source": "tool_log",
        "key_source": "tool_log",
        "call_id": call_id,
        "executor": _text(entry.get("executor")),
        "status": status,
        "sequence": entry.get("sequence"),
        "latency_seconds": entry.get("latency_seconds"),
        "message_index": message_index if message_index is not None else NONE,
        "transcript_output_differs": bool(
            transcript_text is not None and str(transcript_text) != str(output)
        ),
    }
    if metadata["transcript_output_differs"]:
        metadata["transcript_output"] = str(transcript_text)[: CONTEXT_CHARS * 2]
    return {
        "id": ids.observation_id(trace_id, "tool", call_id),
        "traceId": trace_id,
        "parentObservationId": root_id,
        "name": f"tool: {entry.get('tool_name') or 'unknown'}",
        "startTime": started,
        "endTime": ended,
        "input": entry.get("arguments"),
        "output": output,
        "level": "DEFAULT" if success else "ERROR",
        "statusMessage": "" if success else f"tool {status}",
        "metadata": metadata,
    }


def _is_simulated_user(
    message: Mapping[str, Any], index: int, trajectory: Mapping[str, Any], task: Mapping[str, Any]
) -> bool:
    """A ``role: user`` message the user simulator wrote, not the task's pinned opener."""
    if message.get("openrouter_generation_id"):
        return True
    if not role_model(task, "user")[0]:
        return False
    if task.get("interaction_mode") == "agent_only":
        return False
    if index == 0:
        return trajectory.get("first_user_message_source") == "simulator"
    return True


def _user_generation(
    trace_id: str,
    root_id: str,
    index: int,
    message: Mapping[str, Any],
    *,
    context: list[dict[str, Any]],
    started: str | None,
    ended: str | None,
    user_model_name: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": ids.observation_id(trace_id, "ugen", index),
        "traceId": trace_id,
        "parentObservationId": root_id,
        "name": f"user turn {index}",
        "startTime": started,
        "endTime": ended or started,
        "input": context[-CONTEXT_MESSAGES:],
        "output": {"content": message.get("content")},
        "metadata": {
            "role": "user",
            "actor": "user_simulator",
            "message_index": index,
            "openrouter_generation_id": _text(message.get("openrouter_generation_id")),
        },
    }
    if user_model_name:
        body["model"] = user_model_name
    return body


def _agent_observations(
    bundle: Bundle,
    *,
    trace_id: str,
    root_id: str,
    paired: Mapping[int, Mapping[str, Any]],
    usage_match: str,
    start: str | None,
    end: str | None,
    model_name: str | None,
    user_model_name: str | None,
    media: MediaHandler | None,
    stats: ProjectionStats,
) -> list[tuple[str, dict[str, Any]]]:
    messages = [m for m in bundle.trajectory.get("messages") or [] if isinstance(m, Mapping)]
    tool_calls_by_id = {
        call.get("id"): (call.get("name"), call.get("arguments"))
        for message in messages
        for call in (message.get("tool_calls") or [])
        if isinstance(call, Mapping)
    }
    log_entries = {
        str(entry["call_id"]): entry
        for entry in bundle.tool_log
        if entry.get("call_id") not in (None, "")
    }
    emitted_calls: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    context: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        started = _normalize_ts(message.get("ts")) or start
        ended = _normalize_ts(messages[index + 1].get("ts")) if index + 1 < len(messages) else end
        blocks = message.get("content_blocks")
        if role == "user" and _is_simulated_user(message, index, bundle.trajectory, bundle.task):
            out.append(
                (
                    "generation-create",
                    _user_generation(
                        trace_id,
                        root_id,
                        index,
                        message,
                        context=context,
                        started=started,
                        ended=ended,
                        user_model_name=user_model_name,
                    ),
                )
            )
            stats.user_generations += 1
        elif role == "assistant":
            observation_id = ids.observation_id(trace_id, "gen", index)
            output: dict[str, Any] = {
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            }
            if blocks:
                output["content_blocks"] = _replace_media(
                    blocks, trace_id, observation_id, "output", media, stats
                )
            body: dict[str, Any] = {
                "id": observation_id,
                "traceId": trace_id,
                "parentObservationId": root_id,
                "name": f"assistant turn {index}",
                "startTime": started,
                "endTime": ended or started,
                "input": context[-CONTEXT_MESSAGES:],
                "output": output,
                "metadata": {
                    "role": "agent",
                    "message_index": index,
                    "reasoning": message.get("reasoning"),
                    "usage_match": usage_match,
                },
            }
            if model_name:
                body["model"] = model_name
            call = paired.get(index)
            if call is not None:
                details, usage_metadata = usage_fields(call)
                body["usageDetails"] = details
                body["metadata"].update(usage_metadata)
                if call.get("cost_usd") is not None:
                    body["costDetails"] = {"total": call["cost_usd"]}
            out.append(("generation-create", body))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id is not None and str(call_id) in log_entries:
                body = _tool_body_from_log(
                    trace_id,
                    root_id,
                    log_entries[str(call_id)],
                    transcript=message,
                    message_index=index,
                    fallback_start=started,
                )
                if blocks:
                    body["output"] = _replace_media(
                        blocks, trace_id, body["id"], "output", media, stats
                    )
                emitted_calls.add(str(call_id))
                out.append(("span-create", body))
            else:
                # a bundle without a tool log keys its tools by message position (contract v2)
                observation_id = ids.observation_id(trace_id, "tool", ids.tool_key(None, index))
                name, arguments = tool_calls_by_id.get(call_id, (None, None))
                tool_output = (
                    _replace_media(blocks, trace_id, observation_id, "output", media, stats)
                    if blocks
                    else message.get("content")
                )
                out.append(
                    (
                        "span-create",
                        {
                            "id": observation_id,
                            "traceId": trace_id,
                            "parentObservationId": root_id,
                            "name": f"tool: {name or 'unknown'}",
                            "startTime": started,
                            "endTime": ended or started,
                            "input": arguments,
                            "output": tool_output,
                            "metadata": {
                                "role": "agent_tool",
                                "kind": "tool",
                                "source": "transcript",
                                "message_index": index,
                                "call_id": _text(call_id),
                                "key_source": "msg",
                            },
                        },
                    )
                )
        context.append({"role": role, "content": str(message.get("content") or "")[:CONTEXT_CHARS]})
    # tool calls the grader recorded that never reached the transcript (the user simulator's own
    # tools, a call dropped before the tool message was written)
    for call_id, entry in log_entries.items():
        if call_id in emitted_calls:
            continue
        out.append(
            (
                "span-create",
                _tool_body_from_log(
                    trace_id,
                    root_id,
                    entry,
                    transcript=None,
                    message_index=None,
                    fallback_start=start,
                ),
            )
        )
    return out


def _event(
    trace_id: str,
    root_id: str,
    key: str,
    *,
    name: str,
    at: str | None,
    level: str,
    message: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": ids.observation_id(trace_id, "event", key),
        "traceId": trace_id,
        "parentObservationId": root_id,
        "name": name,
        "startTime": at,
        "level": level,
        "statusMessage": message[:500],
        "input": None,
        "output": message[:CONTEXT_CHARS],
        "metadata": {"kind": "event", "event_key": key, **metadata},
    }


def _log_level(level: str) -> str:
    if level in ("ERROR", "CRITICAL"):
        return "ERROR"
    return "WARNING" if level == "WARNING" else "DEFAULT"


def _events(
    bundle: Bundle, *, trace_id: str, root_id: str, include_info: bool
) -> list[dict[str, Any]]:
    """Point-in-time records: ``logs.yaml`` entries (WARNING and ERROR always, INFO on request),
    reply-guard events, a provisioning failure, a run-level budget hit, service captures; keys
    are facts of the bundle so filtering never re-indexes."""
    out: list[dict[str, Any]] = []
    trajectory = bundle.trajectory
    start, _ = trace_time(trajectory)
    end = _normalize_ts(trajectory.get("end_ts")) or start
    logs = bundle.logs.get("logs") if isinstance(bundle.logs, Mapping) else None
    for index, record in enumerate(logs or []):
        if not isinstance(record, Mapping):
            continue
        level = str(record.get("level") or "INFO").upper()
        if level == "INFO" and not include_info:
            continue
        out.append(
            _event(
                trace_id,
                root_id,
                f"log:{index}",
                name=f"log: {str(record.get('message') or '')[:80]}",
                at=_normalize_ts(record.get("timestamp")) or start,
                level=_log_level(level),
                message=str(record.get("message") or ""),
                metadata={
                    "source": "logs.yaml",
                    "log_index": index,
                    "log_level": level,
                    "module": _text(record.get("module")),
                    "context": json.dumps(
                        record.get("context") or {}, ensure_ascii=False, sort_keys=True, default=str
                    ),
                },
            )
        )
    messages = trajectory.get("messages") or []
    for guard in trajectory.get("user_reply_guard_events") or []:
        if not isinstance(guard, Mapping):
            continue
        guard_index = guard.get("message_index")
        outcome = _text(guard.get("outcome"))
        at = start
        if (
            isinstance(guard_index, int)
            and 0 <= guard_index < len(messages)
            and isinstance(messages[guard_index], Mapping)
        ):
            at = _normalize_ts(messages[guard_index].get("ts")) or start
        out.append(
            _event(
                trace_id,
                root_id,
                f"guard:{guard_index}",
                name=f"user reply guard: {outcome}",
                at=at,
                level="ERROR" if outcome == "refused" else "WARNING",
                message=f"user reply guard {outcome} at message {guard_index}",
                metadata={
                    "source": "trajectory.user_reply_guard_events",
                    "message_index": guard_index,
                    "outcome": outcome,
                    "rejected": json.dumps(
                        guard.get("rejected") or [], ensure_ascii=False, default=str
                    ),
                },
            )
        )
    stage = trajectory.get("provision_stage")
    if stage:
        metrics = bundle.metrics
        out.append(
            _event(
                trace_id,
                root_id,
                f"provision:{stage}",
                name=f"provision failure: {stage}",
                at=end,
                level="ERROR",
                message=str(metrics.get("error_reason") or f"provision_error at {stage}"),
                metadata={
                    "source": "trajectory.provision_stage",
                    "stage": _text(stage),
                    "error": _text(metrics.get("error")),
                    "error_reason": _text(metrics.get("error_reason")),
                },
            )
        )
    limit_hit = bundle.limit_hit
    if limit_hit and limit_hit.get("which"):
        out.append(
            _event(
                trace_id,
                root_id,
                f"limit:{limit_hit['which']}",
                name=f"run budget hit: {limit_hit['which']}",
                at=_normalize_ts(limit_hit.get("timestamp")) or end,
                level="ERROR",
                message=(
                    f"run {limit_hit['which']} limit {limit_hit.get('threshold')} hit at "
                    f"{limit_hit.get('value_at_hit')}"
                ),
                metadata={
                    "source": "LIMIT_HIT.json",
                    "which": _text(limit_hit.get("which")),
                    "threshold": limit_hit.get("threshold"),
                    "value_at_hit": limit_hit.get("value_at_hit"),
                },
            )
        )
    captures = bundle.captures
    services = captures.get("services") if isinstance(captures, Mapping) else None
    for service, info in sorted((services or {}).items()):
        out.append(
            _event(
                trace_id,
                root_id,
                f"capture:{service}",
                name=f"service logs captured: {service}",
                at=end,
                level="ERROR",
                message=f"{service}: {_text(captures.get('capture_reason'))}",
                metadata={
                    "source": "services/_capture.yaml",
                    "service": service,
                    "capture_reason": _text(captures.get("capture_reason")),
                    "bytes": (info or {}).get("bytes") if isinstance(info, Mapping) else None,
                    "tail": captures.get("tail"),
                },
            )
        )
    return out


# -- trace metadata ---------------------------------------------------------------------------------


def trace_metadata(
    bundle: Bundle,
    ctx: ProjectionContext,
    *,
    identity: TrialIdentity,
    resolver: ModelNameResolver,
    agent: ModelIdentity | None,
    usage_match: str,
    unpaired_calls: int,
    primary_summary: Mapping[str, Any],
    primary: str | None,
    grading_ids: Sequence[str],
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The fixed trace-metadata schema, every key with an explicit value (the connector's
    ``_trace_metadata``; the deployment's vocabulary enters through ``ctx``)."""
    trajectory, metrics, task = bundle.trajectory, bundle.metrics, bundle.task
    usage = _mapping(metrics.get("usage"))
    tokens_input = metrics.get("tokens_input")
    if tokens_input is None:
        tokens_input = usage.get("prompt_tokens")
    tokens_output = metrics.get("tokens_output")
    if tokens_output is None:
        tokens_output = usage.get("completion_tokens")
    manifest = dict(manifest or {})
    # The slim schema of the offline uploader's ``_trace_metadata`` (PLAN 3.10, revised
    # 2026-09-17): what a reader of the trace page needs and what the uploader's own commands
    # read back. Everything else about the trial lives in the attached files, in the scores and
    # on the grading observation, and the tags carry the deployment's vocabulary. Every key is
    # always present with an explicit value: the receiver merges metadata and an omitted key
    # would persist.
    metadata: dict[str, Any] = {
        # identity
        "task_id": identity.task_id,
        "trial_index": identity.trial_index,
        "run_id": identity.run_id,
        "run_tag": identity.run_tag,
        "attempt": str(identity.attempt_id),
        "label": ctx.label,
        "harness": HARNESS,
        "id_contract": ids.CONTRACT_VERSION,
        # the files on the trace (manifest v2, complete)
        "attach_mode": ctx.attach_mode,
        "attachments_schema": ATTACHMENTS_SCHEMA,
        "attachments": dict(manifest.get("attachments") or {}),
        "attachments_complete": bool(manifest.get("attachments_complete", False)),
        "attachments_skipped": list(manifest.get("attachments_skipped") or []),
        # outcome (mutable across regrades, hence metadata and not tags)
        "status": _text(trajectory.get("status")),
        "termination_reason": _text(trajectory.get("termination_reason")),
        "grading_error": _text(trajectory.get("grading_error")),
        # the verdict: the gradings of the trace and the primary's headline
        "primary_grading": primary if primary else NONE,
        "gradings": json.dumps(list(grading_ids), ensure_ascii=False),
        "grading_count": len(grading_ids),
        **{key: primary_summary[key] for key in TRACE_GRADE_SUMMARY_KEYS},
        # size and spend
        "cost_usd": _number(metrics.get("cost_usd"), 0.0),
        "tokens_input": _number(tokens_input),
        "tokens_output": _number(tokens_output),
        "turns": _number(metrics.get("turns")),
        "tool_calls": _number(metrics.get("tool_calls")),
        "latency_total_s": _number(metrics.get("latency_total_s"), 0.0),
        # the three models, canonical
        "model_name": _text(agent.canonical if agent is not None else None),
        "user_model": _text(_role_canonical(task, "user", resolver)),
        "judge_model": _text(_role_canonical(task, "judge", resolver)),
        # producer bookkeeping
        "upload_mode": UPLOAD_MODE_LIVE,
        "uploader_version": ctx.producer,
        "trace_time_source": TRACE_TIME_SOURCE_LIVE,
    }
    # the caller's keys never override the schema (checked at run start; guarded again here)
    for key, value in ctx.metadata.items():
        if key not in metadata and value is not None:
            metadata[key] = value
    return metadata


def schema_keys() -> frozenset[str]:
    """Every key the projection writes itself; caller and profile metadata may not use them."""
    from tolokaforge_langfuse.model_names import RawModelNameResolver

    identity = TrialIdentity(run_id="r", task_id="t", trial_index=0, attempt_id=0)
    probe = trace_metadata(
        Bundle(trajectory={}),
        ProjectionContext(label="l", session_id="s", tags=()),
        identity=identity,
        resolver=RawModelNameResolver(),
        agent=None,
        usage_match=USAGE_MATCH_UNMATCHED,
        unpaired_calls=0,
        primary_summary=grade_summary({}),
        primary=None,
        grading_ids=(),
        manifest=None,
    )
    return frozenset(probe) | LIVE_ONLY_KEYS


# -- the projection --------------------------------------------------------------------------------


def _grading_events(
    bundle: Bundle,
    *,
    trace_id: str,
    root_id: str,
    run_id: str,
    judge_model_name: str | None,
    at: str | None,
    stats: ProjectionStats,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], dict[str, Any], str]:
    """The run's own grading: (typed events, mirror score bodies, grade summary, grading id)."""
    grade = bundle.grade
    grading_id = ids.live_grading_id(run_id)
    observation_id = ids.grading_observation_id(trace_id, grading_id)
    judge = _judge_observations(
        trace_id,
        grading_id,
        observation_id,
        bundle.judge_messages,
        grade,
        judge_model_name=judge_model_name,
        at=at,
    )
    scores = _score_bodies(trace_id, grade, grading_id=grading_id, observation_id=observation_id)
    mirror = _score_bodies(trace_id, grade, grading_id=grading_id, observation_id=None)
    summary = grade_summary(grade)
    status = summary["judge_status"]
    # provenance as the connector records it for the run's own grading: the grading run id and
    # the trial's end (a bundle fact); everything else was declared by nobody
    provenance: dict[str, Any] = dict.fromkeys(PROVENANCE_KEYS, UNKNOWN)
    provenance["grading_run_id"] = grading_id
    if at:
        provenance["created_at"] = at
    metadata: dict[str, Any] = {
        "kind": OBSERVATION_KIND_GRADING,
        "grading_id": grading_id,
        "source": ids.GRADING_SOURCE_LIVE,
        "content_fingerprint": content_fingerprint(grade),
        "env_sha256": bundle.env_sha256 or NONE,
        "supersedes": NONE,
        "judge_model": judge_model_name or NONE,
        "components": json.dumps(grade.get("components") or {}, sort_keys=True),
        "score_count": len(scores),
        "judge_observation_count": len(judge),
        **summary,
        **{f"provenance_{key}": value for key, value in provenance.items()},
        "attachments": {},
    }
    typed: list[tuple[str, dict[str, Any]]] = [
        (
            "span-create",
            {
                "id": observation_id,
                "traceId": trace_id,
                "parentObservationId": root_id,
                "name": f"grading:{grading_id}",
                "startTime": at,
                "endTime": at,
                "input": _grading_input(bundle.task, grading_id),
                "output": _text(grade.get("reasons")) if grade.get("reasons") else NONE,
                "level": "ERROR" if status in ("errored", "error", "failed") else "DEFAULT",
                "statusMessage": status if status not in ("unspecified", NONE) else "",
                "metadata": metadata,
            },
        )
    ]
    typed.extend(judge)
    typed.extend(("score-create", body) for body in scores)
    stats.judge_observations = len(judge)
    stats.grading_id = grading_id
    return typed, mirror, summary, grading_id


def build_projection(
    identity: TrialIdentity,
    trial_dir: Path,
    ctx: ProjectionContext,
    *,
    resolver: ModelNameResolver,
    manifest: Mapping[str, Any] | None = None,
    media: MediaHandler | None = None,
) -> Projection:
    """Project the persisted trial under ``identity`` into its ingestion events."""
    bundle = load_bundle(Path(trial_dir))
    trace_id = identity.trace_id
    root_id = identity.root_id
    stats = ProjectionStats()
    trajectory = bundle.trajectory
    task_id = str(trajectory.get("task_id") or identity.task_id)
    trial_index: object = trajectory.get("trial_index")
    if trial_index is None:
        trial_index = identity.trial_index
    recorded = ids.trace_id(
        run_tag=identity.run_tag,
        run_id=identity.run_id,
        task_id=task_id,
        trial_index=trial_index,
        attempt=trajectory.get("attempt_id") if trajectory.get("attempt_id") is not None else "na",
    )
    if recorded != trace_id:
        _log.warning(
            "projection: the bundle of %s/%s derives trace %s, the run traced it as %s; the run's"
            " identity wins",
            task_id,
            trial_index,
            recorded,
            trace_id,
        )
    agent_name, agent_provider = agent_model(bundle.task)
    agent = _resolve(resolver, agent_provider, agent_name)
    user_model_name = _role_canonical(bundle.task, "user", resolver)
    judge_model_name = _role_canonical(bundle.task, "judge", resolver)
    messages = [m for m in trajectory.get("messages") or [] if isinstance(m, Mapping)]
    usage = _mapping(bundle.metrics.get("usage"))
    calls = [c for c in (usage.get("calls") or []) if isinstance(c, Mapping)]
    usage_match, paired = pair_usage(messages, calls)
    stats.usage_match = usage_match
    start, _ = trace_time(trajectory)
    end = _normalize_ts(trajectory.get("end_ts"))
    at = end or start

    typed: list[tuple[str, dict[str, Any]]] = []
    typed.append(
        (
            "span-create",
            root_observation(
                trace_id, root_id, trajectory, task_id=task_id, trial_index=trial_index
            ),
        )
    )
    typed.extend(
        _agent_observations(
            bundle,
            trace_id=trace_id,
            root_id=root_id,
            paired=paired,
            usage_match=usage_match,
            start=start,
            end=end,
            model_name=agent.canonical if agent is not None else None,
            user_model_name=user_model_name,
            media=media,
            stats=stats,
        )
    )
    typed.extend(
        ("event-create", body)
        for body in _events(
            bundle, trace_id=trace_id, root_id=root_id, include_info=ctx.attach_mode == "all"
        )
    )
    grading_ids: list[str] = []
    primary: str | None = None
    summary = grade_summary({})
    mirror: list[dict[str, Any]] = []
    if ctx.grades and bundle.grade:
        grading_typed, mirror, summary, grading_id = _grading_events(
            bundle,
            trace_id=trace_id,
            root_id=root_id,
            run_id=identity.run_id,
            judge_model_name=judge_model_name,
            at=at,
            stats=stats,
        )
        typed.extend(grading_typed)
        grading_ids = [grading_id]
        primary = grading_id
    typed.extend(("score-create", body) for body in mirror)

    metadata = trace_metadata(
        bundle,
        ctx,
        identity=identity,
        resolver=resolver,
        agent=agent,
        usage_match=usage_match,
        unpaired_calls=len(calls) - len(paired),
        primary_summary=summary,
        primary=primary,
        grading_ids=grading_ids,
        manifest=manifest,
    )
    trace_body: dict[str, Any] = {
        "id": trace_id,
        "name": f"{ctx.label}/{task_id}",
        "timestamp": start,
        "sessionId": ctx.session_id,
        "input": next((m.get("content") for m in messages if m.get("role") == "user"), None),
        "output": next(
            (
                m.get("content")
                for m in reversed(messages)
                if m.get("role") == "assistant" and m.get("content")
            ),
            None,
        ),
        "tags": list(ctx.tags),
        "metadata": metadata,
        "environment": ctx.environment,
        "release": ctx.release,
        "version": ctx.version,
    }
    events = [envelope("trace-create", trace_body)]
    for kind, body in typed:
        if ctx.environment is not None:
            # observations and scores carry the environment too: the receiver files them under
            # ``default`` otherwise, whatever the trace says
            body.setdefault("environment", ctx.environment)
        events.append(envelope(kind, body))
        if kind == "score-create":
            stats.scores += 1
        elif kind == "event-create":
            stats.events += 1
        else:
            stats.observations += 1
    return Projection(trace_id=trace_id, events=events, trace_body=trace_body, stats=stats)
