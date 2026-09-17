"""The synthetic trial bundle of the golden parity test (ADR-0047, parity amendment; PLAN 3.12,
D29).

This file is committed **byte-identical** in two repositories (the public engine's
``tests/unit/observability/`` and the private connector's ``tests/``): each side projects the
bundle it writes here with its own module and compares the normalised event list against the
shared ``parity_golden.json``. Nothing here is real task content; every value is a neutral
placeholder. Change it on both sides at once and regenerate the golden with the connector
(``python -m tests.gen_parity_golden`` in the connector).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

TASK_ID = "T-001"
TRIAL_INDEX = 0
ATTEMPT_ID = 0
RUN_ID = "acme/pilot/parity/20260917T000000Z"
RUN_TAG = "v1"
LABEL = "pilot_agent"
SESSION_ID = "acme/pilot/pilot_agent/parity/20260917T000000Z"
PROJECT = "pilot-project"
ENVIRONMENT = "development"
# the launcher's caller tags (the connector's core vocabulary; values are placeholders)
CALLER_TAGS = (
    "dataset:pilot",
    "run_kind:test",
    "scope:sample",
    "config:pilot_agent",
    "domain:pilot-domain",
    "ci_run:100",
)
# the tag prefixes the deployment mirrors into trace metadata (the connector writes them always)
MIRROR_PREFIXES = (
    "team",
    "project",
    "dataset",
    "source",
    "run_kind",
    "scope",
    "config",
    "domain",
    "ci_run",
    "ci_chain",
)
CALLER_METADATA = {"model_stem": "pilot_agent", "campaign": "parity"}
TAG_PROFILE_VERSION = "pilot-tags-2026.09.16.1"
AGENT_MODEL = ("openrouter", "acme/pilot-1")
USER_MODEL = ("openrouter", "acme/sim-2")
JUDGE_MODEL = ("openrouter", "acme/judge-3")
# a 1x1 PNG, the one image block of the bundle
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# the metadata keys whose values differ by producer by design (PLAN 3.12), replaced by a
# placeholder before the comparison; ``tag_origins`` names where each tag came from
PRODUCER_KEYS = frozenset(
    {
        "upload_mode",
        "uploader_version",
        "trace_time_source",
        "tag_origins",
        "attach_mode",
        "project_verified",
    }
)
# the native fields that differ by producer (``version``) or by installation (``release``)
PRODUCER_TRACE_FIELDS = frozenset({"version", "release"})
PLACEHOLDER = "<producer>"


def _dump(path: Path, document: Any) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


def trajectory() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "trial_index": TRIAL_INDEX,
        "attempt_id": ATTEMPT_ID,
        "simulator_schema_version": 4,
        "start_ts": "2026-09-17T09:00:00.000000+00:00",
        "end_ts": "2026-09-17T09:00:40.500000+00:00",
        "status": "completed",
        "termination_reason": "user_stop",
        "provision_stage": None,
        "grading_error": None,
        "snapshot_status": {"outcome": "kept"},
        "first_user_message_source": "simulator",
        "messages": [
            {
                "role": "user",
                "content": "Hello, my seat 12A is missing from booking PLT001.",
                "content_blocks": None,
                "tool_calls": None,
                "tool_call_id": None,
                "reasoning": None,
                "openrouter_generation_id": "gen-user-0",
                "ts": "2026-09-17T09:00:01.000000Z",
            },
            {
                "role": "assistant",
                "content": "",
                "content_blocks": None,
                "tool_calls": [
                    {"id": "call_1", "name": "search_booking", "arguments": {"pnr": "PLT001"}}
                ],
                "tool_call_id": None,
                "reasoning": {"summary": "look the booking up"},
                "openrouter_generation_id": "gen-agent-1",
                "ts": "2026-09-17T09:00:05.000000Z",
            },
            {
                "role": "tool",
                "content": '{"pnr": "PLT001", "seats": []}',
                "content_blocks": None,
                "tool_calls": None,
                "tool_call_id": "call_1",
                "reasoning": None,
                "openrouter_generation_id": None,
                "ts": "2026-09-17T09:00:06.000000Z",
            },
            {
                "role": "assistant",
                "content": "",
                "content_blocks": None,
                "tool_calls": [
                    {
                        "id": "call_2",
                        "name": "assign_seat",
                        "arguments": {"pnr": "PLT001", "seat": "12A"},
                    }
                ],
                "tool_call_id": None,
                "reasoning": None,
                "openrouter_generation_id": "gen-agent-2",
                "ts": "2026-09-17T09:00:10.000000Z",
            },
            {
                "role": "tool",
                "content": "Error: seat map unavailable",
                "content_blocks": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(PNG).decode(),
                        },
                    },
                    {"type": "text", "text": "seat map"},
                ],
                "tool_calls": None,
                "tool_call_id": "call_2",
                "reasoning": None,
                "openrouter_generation_id": None,
                "ts": "2026-09-17T09:00:12.000000Z",
            },
            {
                "role": "assistant",
                "content": "I have re-requested the seat map; 12A is now assigned to you.",
                "content_blocks": None,
                "tool_calls": None,
                "tool_call_id": None,
                "reasoning": None,
                "openrouter_generation_id": "gen-agent-3",
                "ts": "2026-09-17T09:00:20.000000Z",
            },
            {
                "role": "user",
                "content": "Thank you. ###STOP###",
                "content_blocks": None,
                "tool_calls": None,
                "tool_call_id": None,
                "reasoning": None,
                "openrouter_generation_id": "gen-user-6",
                "ts": "2026-09-17T09:00:30.000000Z",
            },
        ],
        "user_reply_guard_events": [
            {"message_index": 6, "outcome": "accepted", "rejected": []},
        ],
    }


def metrics() -> dict[str, Any]:
    return {
        "latency_total_s": 39.5,
        "turns": 3,
        "api_calls": 3,
        "usage": {
            "prompt_tokens": 3000,
            "completion_tokens": 90,
            "reasoning_tokens": 30,
            "cached_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
            "calls": [
                {
                    "prompt_tokens": 900,
                    "completion_tokens": 20,
                    "cached_tokens": 0,
                    "reasoning_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cost_usd": 0.001,
                    "cost_source": "provider",
                    "latency_s": 3.0,
                    "openrouter_generation_id": "gen-agent-1",
                },
                {
                    "prompt_tokens": 1000,
                    "completion_tokens": 30,
                    "cached_tokens": 0,
                    "reasoning_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "cost_usd": 0.002,
                    "cost_source": "provider",
                    "latency_s": 4.0,
                    "openrouter_generation_id": "gen-agent-2",
                },
                {
                    "prompt_tokens": 1100,
                    "completion_tokens": 40,
                    "cached_tokens": 0,
                    "reasoning_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cost_usd": 0.003,
                    "cost_source": "provider",
                    "latency_s": 8.0,
                    "openrouter_generation_id": "gen-agent-3",
                },
            ],
        },
        "cost_usd": 0.006,
        "tool_calls": 2,
        "tool_success_rate": 0.5,
        "stuck_detected": False,
        "rate_limit_retries": 0,
        "rate_limit_wait_s": 0.0,
        "redaction": {"policy": "none", "artifacts": [], "omitted": []},
        "schema_version": 4,
        "provisioning_duration_s": 1.25,
    }


def grade() -> dict[str, Any]:
    return {
        "binary_pass": True,
        "score": 0.75,
        "components": {"state_checks": 1.0, "llm_judge": 0.5, "notes": "n/a"},
        "reasons": "Seat assigned; the confirmation lacked the fee note.",
        "criterion_results": [
            {"id": "c1", "met": True, "score": 1.0, "justification": "seat assigned"},
            {"id": "c2", "met": False, "score": 0.0, "justification": "fee note missing"},
        ],
        "trace_check_results": [
            {
                "id": "no_refund",
                "passed": True,
                "severity": "high",
                "kind": "forbidden",
                "weight": 1,
            },
            {
                "id": "gate",
                "passed": False,
                "withheld": True,
                "severity": "low",
                "kind": "gate",
                "weight": 0.5,
            },
        ],
        "trace_checks_summary": {
            "gate_failed": False,
            "winning_path": "main",
            "failed_gate_ids": [],
        },
        "judge_status": "ok",
        "judge_usage": {
            "calls": 1,
            "prompt_tokens": 500,
            "completion_tokens": 60,
            "reasoning_tokens": 20,
            "cost_usd": 0.0015,
            "tool_calls": 0,
            "consistency_rejections": 0,
        },
        "synthesized_by_termination_reason": None,
    }


def task() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "trial_index": TRIAL_INDEX,
        "category": "ancillary",
        "description": "A passenger's paid seat is missing.",
        "interaction_mode": "conversational",
        "initial_user_message": "Hello, my seat 12A is missing from booking PLT001.",
        "user_actor": {"mode": "persona", "persona": "calm-traveller", "backstory": "n/a"},
        "grading_config": {
            "state_checks": {"checks": []},
            "llm_judge": {"rubric": "pilot"},
            "combine": {
                "method": "weighted",
                "pass_threshold": 0.7,
                "weights": {"state_checks": 1, "llm_judge": 1},
            },
        },
        "tools": {"agent": {"enabled": ["search_booking", "assign_seat"]}},
        "policies": {},
        "model_config": {
            "agent": {
                "provider": AGENT_MODEL[0],
                "name": AGENT_MODEL[1],
                "temperature": 0.6,
                "max_tokens": 4096,
                "reasoning": {"mode": "adaptive", "effort_hint": "medium"},
                "resolved": {"effective_preset": "pilot_default", "prompt_policy": "none"},
            },
            "user": {
                "provider": USER_MODEL[0],
                "name": USER_MODEL[1],
                "temperature": 0.0,
                "resolved": {"effective_preset": "sim_default"},
            },
            "judge": {
                "provider": JUDGE_MODEL[0],
                "name": JUDGE_MODEL[1],
                "temperature": 0.0,
                "resolved": {"effective_preset": "judge_default"},
            },
        },
    }


def judge_trajectory() -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": "You judge the transcript."},
            {"role": "user", "content": "Transcript follows."},
            {"role": "assistant", "content": '{"c1": 1.0, "c2": 0.0}', "tool_calls": None},
        ]
    }


def judge_inputs() -> dict[str, Any]:
    return {"state_diff_text": "seats: [] -> [12A]", "read_tools_offered": []}


def tool_log() -> list[dict[str, Any]]:
    return [
        {
            "call_id": "call_1",
            "sequence": 0,
            "tool_name": "search_booking",
            "arguments": {"pnr": "PLT001"},
            "executor": "agent",
            "status": "success",
            "output": '{"pnr": "PLT001", "seats": [], "passengers": ["A. Traveller"]}',
            "latency_seconds": 0.25,
            "timestamp": "2026-09-17T09:00:05.500000Z",
        },
        {
            "call_id": "call_2",
            "sequence": 1,
            "tool_name": "assign_seat",
            "arguments": {"pnr": "PLT001", "seat": "12A"},
            "executor": "agent",
            "status": "error",
            "output": "seat map unavailable",
            "latency_seconds": 1.5,
            "timestamp": "2026-09-17T09:00:10.500000Z",
        },
        {
            "call_id": "call_user_1",
            "sequence": 2,
            "tool_name": "sim_lookup_ticket",
            "arguments": {"pnr": "PLT001"},
            "executor": "user",
            "status": "success",
            "output": '{"status": "assigned"}',
            "latency_seconds": 0.1,
            "timestamp": "2026-09-17T09:00:25.000000Z",
        },
    ]


def logs() -> dict[str, Any]:
    return {
        "trial_id": f"{TASK_ID}:{TRIAL_INDEX}",
        "total_logs": 2,
        "logs": [
            {
                "timestamp": "2026-09-17T09:00:00.100000+00:00",
                "level": "INFO",
                "module": f"{TASK_ID}:{TRIAL_INDEX}",
                "message": "Starting trial execution",
                "context": {"task_id": TASK_ID, "trial_index": TRIAL_INDEX, "max_turns": 20},
            },
            {
                "timestamp": "2026-09-17T09:00:12.100000+00:00",
                "level": "WARNING",
                "module": f"{TASK_ID}:{TRIAL_INDEX}",
                "message": "Tool execution failed",
                "context": {"tool": "assign_seat"},
            },
        ],
    }


def env() -> dict[str, Any]:
    return {
        "environment": {
            "network_policy": "isolated",
            "runner_service": "runner",
            "services": {
                "db": {"image": "acme/db:1.0", "pinned": True},
                "api": {"image": "acme/api:1.0", "pinned": True},
            },
        },
        "agent": {"bookings": [{"pnr": "PLT001", "seats": ["12A"]}]},
        "db": {"bookings": [{"pnr": "PLT001", "seats": ["12A"]}]},
        "filesystem": {},
    }


def write_parity_bundle(run_dir: Path) -> Path:
    """Write the run directory (one trial, the run-level files) and return the trial dir."""
    trial_dir = run_dir / "trials" / TASK_ID / str(TRIAL_INDEX)
    trial_dir.mkdir(parents=True, exist_ok=True)
    _dump(trial_dir / "trajectory.yaml", trajectory())
    _dump(trial_dir / "metrics.yaml", metrics())
    _dump(trial_dir / "grade.yaml", grade())
    _dump(trial_dir / "task.yaml", task())
    _dump(trial_dir / "judge_trajectory.yaml", judge_trajectory())
    _dump(trial_dir / "judge_inputs.yaml", judge_inputs())
    _dump(trial_dir / "tool_log.yaml", tool_log())
    _dump(trial_dir / "logs.yaml", logs())
    _dump(trial_dir / "env.yaml", env())
    _dump(
        trial_dir / "prompts.yaml",
        {
            "system_prompt": "You are a helpful agent.",
            "user_system_prompt": "You are a traveller.",
            "judge_prompt": "Judge.",
        },
    )
    _dump(
        trial_dir / "tools_schemas.yaml",
        {"tools": [{"name": "search_booking"}, {"name": "assign_seat"}]},
    )
    (run_dir / "LIMIT_HIT.json").write_text(
        json.dumps(
            {
                "which": "cost",
                "threshold": 1.0,
                "value_at_hit": 1.02,
                "timestamp": "2026-09-17T09:00:41+00:00",
            }
        )
    )
    (run_dir / "engine_run_state.json").write_text(
        json.dumps(
            {
                "run_id": "pilot-run",
                "presets_file": None,
                "models_fingerprint": {"package_version": "1.0.0", "content_sha256": "f" * 64},
                "adapter_fingerprints": {},
            }
        )
    )
    (run_dir / "run_identity.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "run_tag": RUN_TAG,
                "written_by": "parity",
                "engine_version": "0.0.0",
            }
        )
    )
    return trial_dir


def _normalise_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _normalise_value(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_value(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalise_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The producer-independent view of an event list: envelope ids and timestamps dropped,
    events sorted by (type, body id), keys sorted, tags as a sorted set, the documented producer
    keys and native fields replaced by a placeholder."""
    out: list[dict[str, Any]] = []
    for event in events:
        body = dict(event.get("body") or {})
        if event.get("type") == "trace-create":
            body["tags"] = sorted(body.get("tags") or [])
            for key in PRODUCER_TRACE_FIELDS:
                if key in body:
                    body[key] = PLACEHOLDER
            metadata = dict(body.get("metadata") or {})
            for key in PRODUCER_KEYS:
                if key in metadata:
                    metadata[key] = PLACEHOLDER
            body["metadata"] = metadata
        out.append({"type": event.get("type"), "body": _normalise_value(body)})
    out.sort(key=lambda e: (str(e["type"]), str(e["body"].get("id"))))
    return out
