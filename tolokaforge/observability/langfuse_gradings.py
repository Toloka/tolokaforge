"""The grading of a persisted trial as Langfuse events (ADR-0047, gradings amendment).

The live exporter binds the agent loop, so a live trace ends with the agent's generations and
tool calls. What the bundle knows on top of that, the run's own grading (``grade.yaml``), its
judge transcript (``judge_trajectory.yaml``) and the simulated user's turns
(``trajectory.yaml``), leaves here once the bundle is on disk, through the receiver's ingestion
API and under the id contract the offline connector uses (``ids``). A later connector pass over
the same bundle therefore updates these records instead of duplicating them, and finds the same
content fingerprint on the grading.

Shapes follow the connector's ``mapping.py`` (PLAN 3.6): one ``grading:<id>`` observation under
the root with the judge turns nested beneath and its scores attached (scope ``grading|<id>``),
the trace-level mirror of the same scores (scope ``primary``) and the trace's grading keys.
Nothing here raises for a malformed bundle: a file that cannot be read yields no events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.observability import ids

_log = logging.getLogger(__name__)

NONE = "none"
UNKNOWN = "unknown"
CONTEXT_MESSAGES = 6
CONTEXT_CHARS = 2000
COMMENT_CHARS = 1000
OBSERVATION_KIND_GRADING = "grading"
PROVENANCE_KEYS = (
    "grading_run_id",
    "created_at",
    "engine_version",
    "grader_version",
    "command",
    "rubric_sha256",
    "config_sha256",
    "judge_model",
    "source_bundles",
)


@dataclass
class GradingEvents:
    """The ingestion events of one trial's grading, with the counts the receipt reports."""

    events: list[dict[str, Any]] = field(default_factory=list)
    grading_id: str | None = None
    scores: int = 0
    judge_observations: int = 0
    user_generations: int = 0


# the connector's helpers, kept identical by hand (``mapping.py``): a value the bundle does not
# carry is the literal ``none`` (an omitted metadata key would persist on the receiver), nested
# values are JSON text, a missing number is its default and a present one is left as written


def _text(value: Any) -> str:
    if value is None or value == "":
        return NONE
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _number(value: object, default: float | int = 0) -> Any:
    return default if value is None else value


def _normalize_ts(value: object) -> str | None:
    """A bundle clock as the RFC 3339 text the ingestion API accepts: a YAML ``datetime`` (naive
    stamps are UTC) or the bundle's own text, ``Z`` appended when it carries no zone."""
    if not value:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return (
            datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = str(value)
    return text if text.endswith("Z") or _ZONE_SUFFIX.search(text) else text + "Z"


_ZONE_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")
_clock = _normalize_ts


def content_fingerprint(grade: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of the grade document (the connector's formula)."""
    canonical = json.dumps(grade, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def trace_check_state(result: Mapping[str, Any]) -> str:
    if result.get("withheld"):
        return "withheld"
    if result.get("undecided"):
        return "undecided"
    return "pass" if result.get("passed") else "fail"


def grade_summary(grade: Mapping[str, Any]) -> dict[str, Any]:
    """The grade keys a trace and its grading observation carry, every key explicit."""
    judge_usage = grade.get("judge_usage") or {}
    criteria = grade.get("criterion_results") or []
    checks = grade.get("trace_check_results") or []
    block = grade.get("trace_checks_summary") or {}
    return {
        "pass": grade["binary_pass"] if grade.get("binary_pass") is not None else NONE,
        "score": grade["score"] if grade.get("score") is not None else NONE,
        "synthesized_by_termination_reason": _text(grade.get("synthesized_by_termination_reason")),
        "grade_source": ids.GRADING_SOURCE_LIVE,
        "judge_status": (
            _text(grade.get("judge_status")) if grade.get("judge_status") else "unspecified"
        ),
        "judge_calls": _number(judge_usage.get("calls")),
        "judge_tool_calls": _number(judge_usage.get("tool_calls")),
        "judge_prompt_tokens": _number(judge_usage.get("prompt_tokens")),
        "judge_completion_tokens": _number(judge_usage.get("completion_tokens")),
        "judge_reasoning_tokens": _number(judge_usage.get("reasoning_tokens")),
        "judge_cost_usd": _number(judge_usage.get("cost_usd"), 0.0),
        "judge_consistency_rejections": _number(judge_usage.get("consistency_rejections")),
        "criteria_total": len(criteria),
        "criteria_met": sum(1 for c in criteria if c.get("met")),
        "trace_checks_total": len(checks),
        "trace_checks_passed": sum(1 for c in checks if trace_check_state(c) == "pass"),
        "trace_checks_failed": sum(1 for c in checks if trace_check_state(c) == "fail"),
        "trace_checks_withheld": sum(1 for c in checks if trace_check_state(c) == "withheld"),
        "trace_checks_undecided": sum(1 for c in checks if trace_check_state(c) == "undecided"),
        "trace_checks_gate_failed": bool(block.get("gate_failed")) if block else NONE,
        "trace_checks_winning_path": _text(block.get("winning_path")) if block else NONE,
        "trace_checks_failed_gate_ids": list(block.get("failed_gate_ids") or []),
    }


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _log.warning("gradings: %s not read: %s", path.name, type(exc).__name__)
        return None


def _score_bodies(
    trace_id: str, grade: Mapping[str, Any], *, grading_id: str, observation_id: str | None
) -> list[dict[str, Any]]:
    """Score bodies of the grade on its observation (scope ``grading|<id>``) or as the
    trace-level mirror (scope ``primary``); comment and the stale marker are explicit."""
    bodies: list[dict[str, Any]] = []
    scope = ids.SCORE_SCOPE_PRIMARY if observation_id is None else ids.SCORE_SCOPE_GRADING

    def add(
        name: str,
        value: float | str,
        data_type: str,
        comment: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "id": (
                ids.primary_score_id(trace_id, name)
                if observation_id is None
                else ids.grading_score_id(trace_id, grading_id, name)
            ),
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": data_type,
            "comment": comment or "",
            "metadata": {
                "scope": scope,
                "grading_id": grading_id,
                "stale": False,
                **dict(metadata or {}),
            },
        }
        if observation_id is not None:
            body["observationId"] = observation_id
        bodies.append(body)

    if grade.get("binary_pass") is not None:
        add("binary_pass", 1 if grade["binary_pass"] else 0, "BOOLEAN")
    if grade.get("score") is not None:
        reasons = grade.get("reasons")
        add(
            "score",
            float(grade["score"]),
            "NUMERIC",
            str(_text(reasons))[:COMMENT_CHARS] if reasons else None,
        )
    for name, value in (grade.get("components") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            add(f"component:{name}", float(value), "NUMERIC")
    for criterion in grade.get("criterion_results") or []:
        if not criterion.get("id"):
            continue
        add(
            f"criterion:{criterion['id']}",
            float(criterion.get("score") or 0.0),
            "NUMERIC",
            (criterion.get("justification") or "")[:COMMENT_CHARS] or None,
            {"met": bool(criterion.get("met"))},
        )
    for check in grade.get("trace_check_results") or []:
        if not check.get("id"):
            continue
        add(
            f"trace_check:{check['id']}",
            trace_check_state(check),
            "CATEGORICAL",
            (check.get("message") or "")[:COMMENT_CHARS] or None,
            {
                "severity": _text(check.get("severity")),
                "kind": _text(check.get("kind")),
                "weight": _number(check.get("weight"), 0.0),
                "passed": bool(check.get("passed")),
                "withheld": bool(check.get("withheld")),
                "undecided": bool(check.get("undecided")),
            },
        )
    return bodies


def _judge_usage_fields(judge_usage: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    prompt = int(judge_usage.get("prompt_tokens") or 0)
    completion = int(judge_usage.get("completion_tokens") or 0)
    calls = int(judge_usage.get("calls") or 0)
    details = {"input": prompt, "output": completion, "total": prompt + completion}
    metadata = {
        "usage_source": "aggregate" if calls > 1 else ("exact" if calls == 1 else "unknown"),
        "judge_calls": calls,
        "reasoning_tokens": judge_usage.get("reasoning_tokens"),
    }
    return details, metadata


def _judge_observations(
    trace_id: str,
    grading_id: str,
    grading_observation_id: str,
    judge_messages: Sequence[Mapping[str, Any]],
    grade: Mapping[str, Any],
    *,
    judge_model_name: str | None,
    at: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    """One ``jgen`` generation per judge assistant turn and one ``jtool`` span per judge tool
    result, children of the grading observation; the aggregate judge usage sits on the last
    generation (the bundle records no per-call judge usage)."""
    out: list[tuple[str, dict[str, Any]]] = []
    judge_usage = grade.get("judge_usage") or {}
    assistant_indexes = [i for i, m in enumerate(judge_messages) if m.get("role") == "assistant"]
    common = {"traceId": trace_id, "parentObservationId": grading_observation_id}
    if not assistant_indexes:
        if judge_usage and int(judge_usage.get("calls") or 0) > 0:
            details, usage_metadata = _judge_usage_fields(judge_usage)
            body: dict[str, Any] = {
                "id": ids.observation_id(trace_id, "jgen", grading_id, 0),
                **common,
                "name": "judge (aggregate usage, no transcript)",
                "startTime": at,
                "endTime": at,
                "level": "DEFAULT",
                "metadata": {
                    "role": "judge",
                    "grading_id": grading_id,
                    "message_index": 0,
                    **usage_metadata,
                },
                "usageDetails": details,
                "costDetails": {"total": judge_usage.get("cost_usd") or 0},
            }
            if judge_model_name:
                body["model"] = judge_model_name
            out.append(("generation-create", body))
        return out
    calls_by_id = {
        call.get("id"): (call.get("name"), call.get("arguments"))
        for message in judge_messages
        for call in (message.get("tool_calls") or [])
    }
    seen: dict[str, int] = {}
    for message in judge_messages:
        if message.get("role") == "tool" and message.get("tool_call_id") not in (None, ""):
            cid = str(message["tool_call_id"])
            seen[cid] = seen.get(cid, 0) + 1
    context: list[dict[str, Any]] = []
    for index, message in enumerate(judge_messages):
        role = message.get("role")
        if role == "assistant":
            body = {
                "id": ids.observation_id(trace_id, "jgen", grading_id, index),
                **common,
                "name": f"judge turn {index}",
                "startTime": at,
                "endTime": at,
                "input": context[-CONTEXT_MESSAGES:],
                "output": {
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls"),
                },
                "level": "DEFAULT",
                "metadata": {
                    "role": "judge",
                    "grading_id": grading_id,
                    "message_index": index,
                    "usage_source": NONE,
                },
                "usageDetails": {"input": 0, "output": 0, "total": 0},
                "costDetails": {"total": 0},
            }
            if judge_model_name:
                body["model"] = judge_model_name
            if index == assistant_indexes[-1] and judge_usage:
                details, usage_metadata = _judge_usage_fields(judge_usage)
                body["usageDetails"] = details
                if judge_usage.get("cost_usd") is not None:
                    body["costDetails"] = {"total": judge_usage["cost_usd"]}
                body["metadata"].update(usage_metadata)
            out.append(("generation-create", body))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            unique = call_id not in (None, "") and seen.get(str(call_id)) == 1
            key = str(call_id) if unique else ids.tool_key(None, index)
            name, arguments = calls_by_id.get(call_id, (None, None))
            out.append(
                (
                    "span-create",
                    {
                        "id": ids.observation_id(trace_id, "jtool", grading_id, key),
                        **common,
                        "name": f"judge tool: {name or 'unknown'}",
                        "startTime": at,
                        "endTime": at,
                        "input": arguments,
                        "output": message.get("content"),
                        "level": "DEFAULT",
                        "metadata": {
                            "role": "judge_tool",
                            "grading_id": grading_id,
                            "message_index": index,
                            "call_id": _text(call_id),
                        },
                    },
                )
            )
        context.append({"role": role, "content": str(message.get("content") or "")[:CONTEXT_CHARS]})
    return out


def _is_simulated_user(
    message: Mapping[str, Any], index: int, trajectory: Mapping[str, Any], task: Mapping[str, Any]
) -> bool:
    """The connector's rule: a ``role: user`` message the user simulator wrote, not the task's
    pinned opener."""
    if message.get("openrouter_generation_id"):
        return True
    user_block = (task.get("model_config") or {}).get("user") if isinstance(task, Mapping) else None
    if not user_block:
        return False
    if task.get("interaction_mode") == "agent_only":
        return False
    if index == 0:
        return trajectory.get("first_user_message_source") == "simulator"
    return True


def _user_generations(
    trace_id: str,
    root_id: str,
    trajectory: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    user_model_name: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    context: list[dict[str, Any]] = []
    for index, message in enumerate(trajectory.get("messages") or []):
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "user" and _is_simulated_user(message, index, trajectory, task):
            at = _clock(message.get("ts"))
            body: dict[str, Any] = {
                "id": ids.observation_id(trace_id, "ugen", index),
                "traceId": trace_id,
                "parentObservationId": root_id,
                "name": f"user turn {index}",
                "startTime": at,
                "endTime": at,
                "input": context[-CONTEXT_MESSAGES:],
                "output": {"content": message.get("content")},
                "level": "DEFAULT",
                "metadata": {
                    "role": "user",
                    "actor": "user_simulator",
                    "message_index": index,
                    "openrouter_generation_id": _text(message.get("openrouter_generation_id")),
                },
            }
            if user_model_name:
                body["model"] = user_model_name
            out.append(("generation-create", body))
        context.append({"role": role, "content": str(message.get("content") or "")[:CONTEXT_CHARS]})
    return out


def _task_model(task: Mapping[str, Any], role: str) -> str | None:
    """``task.yaml``'s ``model_config.<role>`` as ``<vendor/model>`` (the name when it already
    carries the vendor, else ``<provider>/<name>``): the fallback when the observer resolved no
    name for the role; the connector's normalizer pass refines it later."""
    config = task.get("model_config") if isinstance(task, Mapping) else None
    block = config.get(role) if isinstance(config, Mapping) else None
    if not isinstance(block, Mapping) or not block.get("name"):
        return None
    name = str(block["name"])
    provider = block.get("provider")
    return name if "/" in name or not provider else f"{provider}/{name}"


def _grading_input(task: Mapping[str, Any], grading_id: str) -> dict[str, Any]:
    raw_config = task.get("grading_config") if isinstance(task, Mapping) else None
    config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else {}
    raw_combine = config.get("combine")
    combine: Mapping[str, Any] = raw_combine if isinstance(raw_combine, Mapping) else {}
    return {
        "grading_kinds": sorted(k for k in config if k != "combine"),
        "combine_method": _text(combine.get("method")),
        "pass_threshold": combine.get("pass_threshold"),
        "weights": combine.get("weights") or {},
        "grading_id": grading_id,
        "source": ids.GRADING_SOURCE_LIVE,
    }


def build_grading_events(
    trace_id: str,
    trial_dir: Path,
    *,
    run_id: str,
    judge_model_name: str | None = None,
    user_model_name: str | None = None,
    env_sha256: str | None = None,
) -> GradingEvents:
    """Every event the persisted bundle adds to its live trace: the simulated user turns, the
    run's grading with its judge transcript and scores, the trace-level mirror and grading keys.
    A bundle without ``grade.yaml`` yields the user turns only."""
    result = GradingEvents()
    root_id = ids.observation_id(trace_id, "root", ids.ROOT_KEY)
    trajectory = _load_yaml(trial_dir / "trajectory.yaml")
    trajectory = trajectory if isinstance(trajectory, Mapping) else {}
    task = _load_yaml(trial_dir / "task.yaml") if (trial_dir / "task.yaml").exists() else {}
    task = task if isinstance(task, Mapping) else {}
    at = _clock(trajectory.get("end_ts"))
    judge_model_name = judge_model_name or _task_model(task, "judge")
    user_model_name = user_model_name or _task_model(task, "user")
    typed: list[tuple[str, dict[str, Any]]] = []

    users = _user_generations(trace_id, root_id, trajectory, task, user_model_name=user_model_name)
    typed.extend(users)
    result.user_generations = len(users)

    grade = _load_yaml(trial_dir / "grade.yaml") if (trial_dir / "grade.yaml").exists() else None
    if isinstance(grade, Mapping) and grade:
        grading_id = ids.live_grading_id(run_id)
        observation_id = ids.grading_observation_id(trace_id, grading_id)
        judge_doc = (
            _load_yaml(trial_dir / "judge_trajectory.yaml")
            if (trial_dir / "judge_trajectory.yaml").exists()
            else None
        )
        judge_messages: list[Mapping[str, Any]] = []
        if isinstance(judge_doc, Mapping) and isinstance(judge_doc.get("messages"), list):
            judge_messages = [m for m in judge_doc["messages"] if isinstance(m, Mapping)]
        elif isinstance(judge_doc, list):
            judge_messages = [m for m in judge_doc if isinstance(m, Mapping)]
        judge = _judge_observations(
            trace_id,
            grading_id,
            observation_id,
            judge_messages,
            grade,
            judge_model_name=judge_model_name,
            at=at,
        )
        scores = _score_bodies(
            trace_id, grade, grading_id=grading_id, observation_id=observation_id
        )
        mirror = _score_bodies(trace_id, grade, grading_id=grading_id, observation_id=None)
        summary = grade_summary(grade)
        status = summary["judge_status"]
        provenance = dict.fromkeys(PROVENANCE_KEYS, UNKNOWN)
        provenance["grading_run_id"] = grading_id
        if at:
            provenance["created_at"] = at
        metadata: dict[str, Any] = {
            "kind": OBSERVATION_KIND_GRADING,
            "grading_id": grading_id,
            "source": ids.GRADING_SOURCE_LIVE,
            "content_fingerprint": content_fingerprint(grade),
            "env_sha256": env_sha256 or NONE,
            "supersedes": NONE,
            "judge_model": judge_model_name or NONE,
            "components": json.dumps(grade.get("components") or {}, sort_keys=True),
            "score_count": len(scores),
            "judge_observation_count": len(judge),
            **summary,
            **{f"provenance_{key}": value for key, value in provenance.items()},
            "attachments": {},
        }
        typed.append(
            (
                "span-create",
                {
                    "id": observation_id,
                    "traceId": trace_id,
                    "parentObservationId": root_id,
                    "name": f"grading:{grading_id}",
                    "startTime": at,
                    "endTime": at,
                    "input": _grading_input(task, grading_id),
                    "output": _text(grade.get("reasons")) if grade.get("reasons") else NONE,
                    "level": "ERROR" if status in ("errored", "error", "failed") else "DEFAULT",
                    "statusMessage": status if status not in ("unspecified", NONE) else "",
                    "metadata": metadata,
                },
            )
        )
        typed.extend(judge)
        typed.extend(("score-create", body) for body in scores)
        typed.extend(("score-create", body) for body in mirror)
        typed.append(
            (
                "trace-create",
                {
                    "id": trace_id,
                    "metadata": {
                        **summary,
                        "primary_grading": grading_id,
                        "gradings": json.dumps([grading_id]),
                        "grading_count": 1,
                    },
                },
            )
        )
        result.grading_id = grading_id
        result.scores = len(scores) + len(mirror)
        result.judge_observations = len(judge)

    now = datetime.now(timezone.utc).isoformat()
    result.events = [
        {"id": uuid.uuid4().hex, "type": kind, "timestamp": now, "body": body}
        for kind, body in typed
    ]
    return result
