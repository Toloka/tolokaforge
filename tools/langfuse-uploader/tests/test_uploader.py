"""Tests for langfuse_uploader — trial bundle mapping, media handling, batching, discovery."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml
from langfuse_uploader.cli import app as cli_app
from langfuse_uploader.uploader import (
    LangfuseClient,
    TrialStats,
    build_trial_events,
    discover_trials,
    iter_batches,
    upload_trial,
)
from typer.testing import CliRunner

pytestmark = pytest.mark.unit


def make_trajectory(**overrides) -> dict:
    trajectory = {
        "task_id": "task-alpha",
        "trial_index": 0,
        "status": "completed",
        "termination_reason": "final_answer",
        "start_ts": "2026-01-01T00:00:00",
        "end_ts": "2026-01-01T00:01:00",
        "messages": [
            {"role": "user", "content": "count files", "ts": "2026-01-01T00:00:00"},
            {
                "role": "assistant",
                "content": None,
                "ts": "2026-01-01T00:00:10",
                "tool_calls": [
                    {"id": "call-1", "name": "shell", "arguments": {"cmd": "ls | wc -l"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "3", "ts": "2026-01-01T00:00:20"},
            {"role": "assistant", "content": "There are 3 files.", "ts": "2026-01-01T00:00:30"},
        ],
    }
    trajectory.update(overrides)
    return trajectory


METRICS = {
    "turns": 2,
    "api_calls": 2,
    "tool_calls": 1,
    "tool_success_rate": 1.0,
    "tokens_input": 100,
    "tokens_output": 20,
    "cost_usd": 0.01,
    "latency_total_s": 60.0,
    "usage": {
        "calls": [
            {"prompt_tokens": 40, "completion_tokens": 5, "cost_usd": 0.004},
            {"prompt_tokens": 60, "completion_tokens": 15, "cost_usd": 0.006},
        ]
    },
}

GRADE = {
    "binary_pass": True,
    "score": 0.9,
    "reasons": "solved it",
    "components": {"state_checks": 1.0, "notes": "n/a"},
}


def build(trajectory: dict | None = None, **kwargs):
    return build_trial_events(
        trajectory or make_trajectory(), METRICS, GRADE, label="demo", session="run-1", **kwargs
    )


class TestBuildTrialEvents:
    def test_trace_event(self) -> None:
        events, trace_id = build()
        trace = events[0]
        assert trace["type"] == "trace-create"
        body = trace["body"]
        assert body["id"] == trace_id
        assert body["name"] == "demo/task-alpha"
        assert body["sessionId"] == "run-1"
        assert body["input"] == "count files"
        assert body["output"] == "There are 3 files."
        assert {"tolokaforge", "demo", "status:completed", "pass:True"} <= set(body["tags"])
        assert body["metadata"]["cost_usd"] == 0.01
        assert body["metadata"]["task_id"] == "task-alpha"
        assert body["timestamp"] == "2026-01-01T00:00:00Z"  # naive ts treated as UTC

    def test_generations_and_spans(self) -> None:
        events, _ = build()
        generations = [e for e in events if e["type"] == "generation-create"]
        spans = [e for e in events if e["type"] == "span-create"]
        assert len(generations) == 2
        assert generations[0]["body"]["usageDetails"] == {"input": 40, "output": 5}
        assert generations[0]["body"]["costDetails"] == {"total": 0.004}
        assert generations[1]["body"]["usageDetails"] == {"input": 60, "output": 15}
        assert len(spans) == 1
        assert spans[0]["body"]["name"] == "tool: shell"
        assert spans[0]["body"]["input"] == {"cmd": "ls | wc -l"}
        assert spans[0]["body"]["output"] == "3"

    def test_scores(self) -> None:
        events, _ = build()
        scores = {e["body"]["name"]: e["body"] for e in events if e["type"] == "score-create"}
        assert scores["binary_pass"]["value"] == 1
        assert scores["binary_pass"]["dataType"] == "BOOLEAN"
        assert scores["score"]["value"] == 0.9
        assert scores["score"]["comment"] == "solved it"
        assert scores["component:state_checks"]["value"] == 1.0
        assert "component:notes" not in scores  # non-numeric components are skipped

    def test_ids_are_deterministic(self) -> None:
        first, trace_a = build()
        second, trace_b = build()
        assert trace_a == trace_b
        assert [e["body"]["id"] for e in first] == [e["body"]["id"] for e in second]
        _, trace_c = build(run_tag="v2")
        assert trace_c != trace_a


def image_block() -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"fake-png").decode(),
        },
    }


def trajectory_with_image() -> dict:
    trajectory = make_trajectory()
    trajectory["messages"][2]["content_blocks"] = [image_block(), {"type": "text", "text": "hi"}]
    return trajectory


class TestMedia:
    def test_handler_token_replaces_block(self) -> None:
        calls = []

        def handler(trace_id, observation_id, field, content_type, raw):
            calls.append((field, content_type, raw))
            return "@@@langfuseMedia:type=image/png|id=m1|source=bytes@@@"

        stats = TrialStats()
        events, _ = build(trajectory_with_image(), media_handler=handler, stats=stats)
        span = next(e for e in events if e["type"] == "span-create")
        assert span["body"]["output"][0].startswith("@@@langfuseMedia:")
        assert span["body"]["output"][1] == {"type": "text", "text": "hi"}
        assert calls == [("output", "image/png", b"fake-png")]
        assert stats.media_uploaded == 1

    def test_handler_failure_is_fail_open(self) -> None:
        def handler(*args):
            raise RuntimeError("boom")

        stats = TrialStats()
        events, _ = build(trajectory_with_image(), media_handler=handler, stats=stats)
        span = next(e for e in events if e["type"] == "span-create")
        assert span["body"]["output"][0]["note"] == "media upload failed: boom"
        assert stats.media_failed == 1

    def test_no_handler_strips_base64(self) -> None:
        events, _ = build(trajectory_with_image())
        span = next(e for e in events if e["type"] == "span-create")
        assert span["body"]["output"][0] == {
            "type": "image",
            "note": "media stripped",
            "media_type": "image/png",
        }
        assert base64.b64encode(b"fake-png").decode() not in json.dumps(events)


class TestIterBatches:
    def test_splits_and_preserves_order(self) -> None:
        events = [{"id": str(i), "payload": "x" * 1000} for i in range(10)]
        batches = list(iter_batches(events, max_bytes=3000))
        assert len(batches) > 1
        assert [e["id"] for batch in batches for e in batch] == [str(i) for i in range(10)]
        assert all(len(json.dumps(batch).encode()) <= 3200 for batch in batches)

    def test_single_oversized_event_still_yields(self) -> None:
        events = [{"payload": "x" * 5000}]
        assert list(iter_batches(events, max_bytes=100)) == [events]


def write_bundle(trial_dir: Path, trajectory: dict) -> None:
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory))
    (trial_dir / "metrics.yaml").write_text(yaml.safe_dump(METRICS))
    (trial_dir / "grade.yaml").write_text(yaml.safe_dump(GRADE))


class TestDiscoverTrials:
    def test_run_dir_layout(self, tmp_path: Path) -> None:
        write_bundle(tmp_path / "trials" / "task-a" / "0", make_trajectory())
        write_bundle(tmp_path / "trials" / "task-b" / "1", make_trajectory(task_id="task-b"))
        found = discover_trials(tmp_path)
        assert [(p.parent.name, p.name) for p in found] == [("task-a", "0"), ("task-b", "1")]

    def test_single_trial_dir(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, make_trajectory())
        assert discover_trials(tmp_path) == [tmp_path]

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert discover_trials(tmp_path) == []


def recording_client(errors: list | None = None) -> tuple[LangfuseClient, list]:
    client = LangfuseClient(host="http://localhost:3000", public_key="pk", secret_key="sk")
    posted: list = []

    def fake_api(method, path, body=None):
        posted.append((method, path, body))
        return {"successes": body["batch"], "errors": errors or []}

    client.api = fake_api  # type: ignore[method-assign]
    return client, posted


class TestUploadTrial:
    def test_posts_ingestion_batches(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, make_trajectory())
        client, posted = recording_client()
        result = upload_trial(client, tmp_path, label="demo", session="run-1")
        assert result.status == "ok"
        assert all(path == "/api/public/ingestion" for _, path, _ in posted)
        assert sum(len(body["batch"]) for _, _, body in posted) == result.events

    def test_ingestion_errors_reported(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, make_trajectory())
        client, _ = recording_client(errors=[{"id": "e1", "message": "bad event"}])
        result = upload_trial(client, tmp_path, label="demo", session="run-1")
        assert result.status == "error"
        assert "bad event" in (result.error or "")

    def test_missing_trajectory_is_empty(self, tmp_path: Path) -> None:
        client, posted = recording_client()
        result = upload_trial(client, tmp_path, label="demo", session="run-1")
        assert result.status == "empty"
        assert posted == []


class TestDefaultRunName:
    def test_run_dir_uses_own_name(self, tmp_path: Path) -> None:
        from langfuse_uploader.cli import _default_run_name

        run_dir = tmp_path / "run-42"
        write_bundle(run_dir / "trials" / "task-a" / "0", make_trajectory())
        assert _default_run_name(run_dir) == "run-42"

    def test_single_trial_dir_walks_up_to_run(self, tmp_path: Path) -> None:
        from langfuse_uploader.cli import _default_run_name

        trial_dir = tmp_path / "run-42" / "trials" / "task-a" / "0"
        write_bundle(trial_dir, make_trajectory())
        assert _default_run_name(trial_dir) == "run-42"

    def test_dot_resolves_to_real_name(self, tmp_path: Path, monkeypatch) -> None:
        from langfuse_uploader.cli import _default_run_name

        run_dir = tmp_path / "run-42"
        run_dir.mkdir()
        monkeypatch.chdir(run_dir)
        assert _default_run_name(Path(".")) == "run-42"


class TestCli:
    def test_help_lists_commands(self) -> None:
        result = CliRunner().invoke(cli_app, ["--help"])
        assert result.exit_code == 0
        assert "upload" in result.output
        assert "watch" in result.output

    def test_upload_requires_credentials(self, tmp_path: Path, monkeypatch) -> None:
        for var in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        result = CliRunner().invoke(cli_app, ["upload", str(tmp_path)])
        assert result.exit_code == 1
        assert "Missing configuration" in result.output
