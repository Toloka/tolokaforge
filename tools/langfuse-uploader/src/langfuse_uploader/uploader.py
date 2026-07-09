"""Map tolokaforge trial bundles to Langfuse ingestion events and upload them.

A trial bundle is the directory ``<run_dir>/trials/<task_id>/<trial_index>/`` produced by
``tolokaforge run``, containing ``trajectory.yaml``, ``metrics.yaml`` and ``grade.yaml``.

Mapping:
    run               -> Langfuse session (``sessionId``) + tags
    trial             -> trace
    assistant message -> generation (per-turn usage from ``metrics.usage.calls``)
    tool message      -> span
    grade             -> scores
    metrics           -> trace metadata

Trace, observation and score ids are deterministic (uuid5 over run tag / label / task id /
trial index), so re-uploading the same trial updates the existing trace instead of
duplicating it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Namespace for deterministic trace/observation/score ids.
_UUID_NS = uuid.UUID("00000000-0000-0000-0000-00000000f00d")

# Langfuse rejects ingestion bodies over ~4.5 MB (server-side cap); flush batches well below it.
MAX_BATCH_BYTES = 3_500_000

# (trace_id, observation_id, field, content_type, raw_bytes) -> media reference token or None.
MediaHandler = Callable[[str, str, str, str, bytes], "str | None"]


def deterministic_id(*parts: object) -> str:
    return str(uuid.uuid5(_UUID_NS, ":".join(str(part) for part in parts)))


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _normalize_ts(value: object) -> str | None:
    if not value:
        return None
    text = str(value)
    # Bundle timestamps carry no timezone marker -> treat them as UTC.
    return text if text.endswith("Z") or "+" in text else text + "Z"


@dataclass
class LangfuseClient:
    """Minimal stdlib-only client for the Langfuse public REST API (Basic auth)."""

    host: str
    public_key: str
    secret_key: str
    # "host:port" override for presigned media PUTs (e.g. a kubectl port-forward) when the
    # object store hostname inside the URL is not directly reachable from this machine.
    media_put_via: str | None = None
    timeout: float = 60.0

    def __post_init__(self) -> None:
        self.host = self.host.rstrip("/")

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.host}{path}", data=data, method=method)
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        if data:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def upload_media(
        self, trace_id: str, observation_id: str, field: str, content_type: str, raw: bytes
    ) -> str | None:
        """Register media, PUT the bytes to the presigned URL, confirm the upload.

        Returns the LangfuseMedia reference token, or None when no media id was issued.
        """
        sha256 = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        created = self.api(
            "POST",
            "/api/public/media",
            {
                "traceId": trace_id,
                "observationId": observation_id,
                "field": field,
                "contentType": content_type,
                "contentLength": len(raw),
                "sha256Hash": sha256,
            },
        )
        media_id = created.get("mediaId")
        upload_url = created.get("uploadUrl")  # absent when these bytes are already stored
        if media_id and upload_url:
            status = self._put_presigned(upload_url, content_type, sha256, raw)
            try:
                self.api(
                    "PATCH",
                    f"/api/public/media/{media_id}",
                    {
                        "uploadedAt": datetime.now(timezone.utc).isoformat(),
                        "uploadHttpStatus": status,
                        "uploadHttpError": None,
                    },
                )
            except Exception:
                pass  # confirmation is best-effort; the bytes are already stored
        if not media_id:
            return None
        return f"@@@langfuseMedia:type={content_type}|id={media_id}|source=bytes@@@"

    def _put_presigned(self, url: str, content_type: str, sha256: str, raw: bytes) -> int:
        parsed = urllib.parse.urlparse(url)
        if self.media_put_via:
            via_host, _, via_port = self.media_put_via.partition(":")
            connect_host, connect_port = via_host, int(via_port)
        else:
            connect_host = parsed.hostname or ""
            connect_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_cls(connect_host, connect_port, timeout=120)
        try:
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            # The signature covers the original Host header, so send it explicitly even when
            # media_put_via routes the TCP connection elsewhere.
            conn.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", parsed.netloc)
            conn.putheader("Content-Type", content_type)
            conn.putheader("x-amz-checksum-sha256", sha256)
            conn.putheader("Content-Length", str(len(raw)))
            conn.endheaders()
            conn.send(raw)
            return conn.getresponse().status
        finally:
            conn.close()


@dataclass
class TrialStats:
    media_uploaded: int = 0
    media_failed: int = 0


def _replace_media(
    obj: Any,
    trace_id: str,
    observation_id: str,
    field: str,
    media_handler: MediaHandler | None,
    stats: TrialStats,
) -> Any:
    """Recursively replace base64 image blocks with Langfuse media reference tokens.

    Without a handler (or when the upload fails) the block becomes a small placeholder, so
    raw base64 payloads never end up in the ingestion body.
    """
    if isinstance(obj, dict):
        source = obj.get("source")
        if (
            obj.get("type") == "image"
            and isinstance(source, dict)
            and source.get("type") == "base64"
            and source.get("data")
        ):
            content_type = source.get("media_type", "image/png")
            if media_handler is not None:
                try:
                    raw = base64.b64decode(source["data"])
                    token = media_handler(trace_id, observation_id, field, content_type, raw)
                    if token:
                        stats.media_uploaded += 1
                        return token
                except Exception as exc:
                    stats.media_failed += 1
                    return {
                        "type": "image",
                        "note": f"media upload failed: {exc}",
                        "media_type": content_type,
                    }
            return {"type": "image", "note": "media stripped", "media_type": content_type}
        return {
            key: _replace_media(value, trace_id, observation_id, field, media_handler, stats)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [
            _replace_media(item, trace_id, observation_id, field, media_handler, stats)
            for item in obj
        ]
    return obj


def build_trial_events(
    trajectory: dict[str, Any],
    metrics: dict[str, Any],
    grade: dict[str, Any],
    *,
    label: str,
    session: str,
    run_tag: str = "v1",
    media_handler: MediaHandler | None = None,
    stats: TrialStats | None = None,
    fallback_task_id: str = "unknown",
) -> tuple[list[dict[str, Any]], str]:
    """Build the Langfuse ingestion event batch for one trial bundle."""
    stats = stats if stats is not None else TrialStats()
    task_id = str(trajectory.get("task_id", fallback_task_id))
    trial_index = trajectory.get("trial_index", 0)
    trace_id = deterministic_id(run_tag, label, task_id, trial_index)
    start = _normalize_ts(trajectory.get("start_ts"))
    end = _normalize_ts(trajectory.get("end_ts"))
    messages = trajectory.get("messages") or []
    tool_calls_by_id = {
        call.get("id"): (call.get("name"), call.get("arguments"))
        for message in messages
        for call in (message.get("tool_calls") or [])
    }
    usage_calls = (metrics.get("usage") or {}).get("calls") or []
    events: list[dict[str, Any]] = []

    def add_event(event_type: str, body: dict[str, Any]) -> None:
        events.append(
            {
                "id": str(uuid.uuid4()),
                "type": event_type,
                "timestamp": start or "1970-01-01T00:00:00Z",
                "body": body,
            }
        )

    metric_keys = (
        "turns",
        "api_calls",
        "tool_calls",
        "tool_success_rate",
        "cost_usd",
        "latency_total_s",
    )
    # Token totals live at metrics.tokens_{input,output} in some runs and under metrics.usage
    # (prompt_tokens/completion_tokens) in others; fall back so the trace-level totals are populated.
    usage = metrics.get("usage") or {}
    tokens_input = metrics.get("tokens_input")
    if tokens_input is None:
        tokens_input = usage.get("prompt_tokens")
    tokens_output = metrics.get("tokens_output")
    if tokens_output is None:
        tokens_output = usage.get("completion_tokens")
    add_event(
        "trace-create",
        {
            "id": trace_id,
            "name": f"{label}/{task_id[:12]}",
            "timestamp": start,
            "sessionId": session,
            "input": next((m.get("content") for m in messages if m.get("role") == "user"), None),
            "output": next(
                (
                    m.get("content")
                    for m in reversed(messages)
                    if m.get("role") == "assistant" and m.get("content")
                ),
                None,
            ),
            "tags": [
                "tolokaforge",
                label,
                f"status:{trajectory.get('status')}",
                f"pass:{grade.get('binary_pass')}",
            ],
            "metadata": {key: metrics.get(key) for key in metric_keys}
            | {
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "task_id": task_id,
                "trial_index": trial_index,
                "status": trajectory.get("status"),
                "termination_reason": trajectory.get("termination_reason"),
            },
        },
    )

    context: list[dict[str, Any]] = []
    assistant_turn = 0
    for index, message in enumerate(messages):
        role = message.get("role")
        started = _normalize_ts(message.get("ts")) or start
        ended = _normalize_ts(messages[index + 1].get("ts")) if index + 1 < len(messages) else end
        blocks = message.get("content_blocks")
        if role == "assistant":
            observation_id = deterministic_id(trace_id, "gen", index)
            output: Any = {
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            }
            if blocks:
                output["content_blocks"] = _replace_media(
                    blocks, trace_id, observation_id, "output", media_handler, stats
                )
            body = {
                "id": observation_id,
                "traceId": trace_id,
                "name": f"assistant turn {index}",
                "startTime": started,
                "endTime": ended or started,
                "input": context[-6:],
                "output": output,
                "metadata": {"reasoning": message.get("reasoning")},
            }
            call = usage_calls[assistant_turn] if assistant_turn < len(usage_calls) else None
            if call:
                body["usageDetails"] = {
                    "input": call.get("prompt_tokens"),
                    "output": call.get("completion_tokens"),
                }
                if call.get("cost_usd") is not None:
                    body["costDetails"] = {"total": call["cost_usd"]}
            assistant_turn += 1
            add_event("generation-create", body)
        elif role == "tool":
            observation_id = deterministic_id(trace_id, "tool", index)
            name, arguments = tool_calls_by_id.get(message.get("tool_call_id"), (None, None))
            output = (
                _replace_media(blocks, trace_id, observation_id, "output", media_handler, stats)
                if blocks
                else message.get("content")
            )
            add_event(
                "span-create",
                {
                    "id": observation_id,
                    "traceId": trace_id,
                    "name": f"tool: {name or 'unknown'}",
                    "startTime": started,
                    "endTime": ended or started,
                    "input": arguments,
                    "output": output,
                },
            )
        context.append({"role": role, "content": (message.get("content") or "")[:2000]})

    def add_score(name: str, value: float, data_type: str, comment: str | None = None) -> None:
        add_event(
            "score-create",
            {
                "id": deterministic_id(trace_id, "score", name),
                "traceId": trace_id,
                "name": name,
                "value": value,
                "dataType": data_type,
                "comment": comment,
            },
        )

    if grade.get("binary_pass") is not None:
        add_score("binary_pass", 1 if grade["binary_pass"] else 0, "BOOLEAN")
    if grade.get("score") is not None:
        add_score("score", float(grade["score"]), "NUMERIC", str(grade.get("reasons") or "")[:900])
    for name, value in (grade.get("components") or {}).items():
        if isinstance(value, (int, float)):
            add_score(f"component:{name}", float(value), "NUMERIC")

    return events, trace_id


def iter_batches(
    events: list[dict[str, Any]], max_bytes: int = MAX_BATCH_BYTES
) -> Iterator[list[dict[str, Any]]]:
    """Split events into ingestion batches whose serialized size stays under max_bytes."""
    batch: list[dict[str, Any]] = []
    size = 0
    for event in events:
        event_size = len(json.dumps(event).encode()) + 1
        if batch and size + event_size > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(event)
        size += event_size
    if batch:
        yield batch


def discover_trials(root: Path) -> list[Path]:
    """Trial bundle dirs under a run output dir; a trial dir itself is also accepted."""
    if (root / "trajectory.yaml").exists():
        return [root]
    return sorted(path.parent for path in root.glob("trials/*/*/trajectory.yaml"))


@dataclass
class TrialResult:
    trial_dir: Path
    status: str  # "ok" | "error" | "empty"
    trace_id: str | None = None
    events: int = 0
    media_uploaded: int = 0
    media_failed: int = 0
    error: str | None = None


def upload_trial(
    client: LangfuseClient,
    trial_dir: Path,
    *,
    label: str,
    session: str,
    run_tag: str = "v1",
) -> TrialResult:
    """Upload one trial bundle. Media failures are tolerated; transport errors raise."""
    trajectory = load_yaml(trial_dir / "trajectory.yaml")
    if not trajectory:
        return TrialResult(trial_dir, "empty")
    metrics = load_yaml(trial_dir / "metrics.yaml")
    grade = load_yaml(trial_dir / "grade.yaml")
    stats = TrialStats()
    events, trace_id = build_trial_events(
        trajectory,
        metrics,
        grade,
        label=label,
        session=session,
        run_tag=run_tag,
        media_handler=client.upload_media,
        stats=stats,
        fallback_task_id=trial_dir.parent.name,
    )
    errors: list[Any] = []
    for batch in iter_batches(events):
        response = client.api("POST", "/api/public/ingestion", {"batch": batch})
        errors.extend(response.get("errors") or [])
    return TrialResult(
        trial_dir,
        "ok" if not errors else "error",
        trace_id=trace_id,
        events=len(events),
        media_uploaded=stats.media_uploaded,
        media_failed=stats.media_failed,
        error=json.dumps(errors[:1]) if errors else None,
    )
