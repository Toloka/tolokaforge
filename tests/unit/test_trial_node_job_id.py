"""Unit locks for the caller job id on ``TrialNodeInputs``.

The field is optional and carries a caller-supplied unique id. When set it
scopes the trial's on-disk output under ``<output_dir>/<job_id>`` and keys the
trace run identity; when absent, the output path and the trace identity are
exactly as without it. These tests pin the field, the output-path helper and the
trace-identity integration in isolation — no backend, Docker or LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_trial_spec
from tolokaforge.core.models import ObservabilityConfig, TaskConfig
from tolokaforge.core.trial import TrialNodeInputs
from tolokaforge.core.trial_node import _job_scoped_output_dir, _resolve_trial_tracing

pytestmark = pytest.mark.unit


def _inputs(*, job_id: str | None = None) -> TrialNodeInputs:
    return TrialNodeInputs(
        task=TaskConfig(
            task_id="t",
            name="t",
            category="test",
            description="d",
            initial_user_message="hi",
        ),
        trial_spec=make_trial_spec(trial_id="t:0", task_id="t"),
        job_id=job_id,
        grpc_max_message_mb=50,
        runtime="auto",
        conductor="in_process",
    )


def test_job_id_defaults_to_none() -> None:
    assert _inputs().job_id is None


def test_job_id_round_trips_through_json() -> None:
    inputs = _inputs(job_id="wf-run-7:t:0")
    rebuilt = TrialNodeInputs.model_validate_json(inputs.model_dump_json())
    assert rebuilt.job_id == "wf-run-7:t:0"


def test_old_inputs_without_job_id_still_validate() -> None:
    # extra="forbid" with an optional field: a payload that never mentions job_id
    # (an input serialized before the field existed) validates and reads as None.
    payload = _inputs().model_dump()
    payload.pop("job_id")
    assert TrialNodeInputs.model_validate(payload).job_id is None


def test_output_dir_nests_under_the_job_id_when_both_are_set() -> None:
    assert _job_scoped_output_dir(Path("/out"), "job-abc") == Path("/out/job-abc")


def test_output_dir_is_unchanged_without_a_job_id() -> None:
    assert _job_scoped_output_dir(Path("/out"), None) == Path("/out")


def test_no_output_dir_stays_none_regardless_of_job_id() -> None:
    assert _job_scoped_output_dir(None, "job-abc") is None
    assert _job_scoped_output_dir(None, None) is None


def test_trace_identity_carries_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # No tracing switch on: build returns the null observer but still the run
    # identity, whose run_id is the trace's run id. A caller job id keys it.
    for name in list(__import__("os").environ):
        if name.endswith("_TRACING_ENABLED"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_ID", raising=False)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_TAG", raising=False)

    _, identity = _resolve_trial_tracing(
        ObservabilityConfig(), run_id="engine-run", job_id="job-abc", output_dir=None
    )
    assert identity is not None
    assert identity.run_id == "job-abc"


def test_trace_identity_falls_back_to_the_run_id_without_a_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(__import__("os").environ):
        if name.endswith("_TRACING_ENABLED"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_ID", raising=False)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_TAG", raising=False)

    _, identity = _resolve_trial_tracing(
        ObservabilityConfig(), run_id="engine-run", job_id=None, output_dir=None
    )
    assert identity is not None
    assert identity.run_id == "engine-run"
