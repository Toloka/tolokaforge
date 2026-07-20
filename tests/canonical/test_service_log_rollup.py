"""Behaviour lock for the run-level captured-service-log collector.

Drives :func:`collect_service_log_captures` against synthetic run-output
trees covering all three capture surfaces (per-trial provision-fail
``_capture.yaml``, per-trial trial-body ``metrics.yaml``, run-level
shared-stack ``services/_capture.yaml``), the aggregation arithmetic
(per-service sum across entries, grand total), and the fail-safe contract
(malformed / missing / mistyped inputs are skipped, never raised).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.compose_materialisation import run_services_dir, write_capture_manifest
from tolokaforge.core.output.aggregate_models import ServiceLogCaptureSource
from tolokaforge.core.output.service_log_rollup import collect_service_log_captures

pytestmark = pytest.mark.canonical


def _write_provision_capture(
    output_dir: Path,
    task_id: str,
    trial_index: int,
    captured: dict[str, int],
    *,
    capture_reason: str = "provision_error",
) -> None:
    dest = output_dir / "trials" / task_id / str(trial_index) / "services"
    dest.mkdir(parents=True)
    write_capture_manifest(dest, tail=500, captured=captured, capture_reason=capture_reason)


def _write_trial_body_metrics(
    output_dir: Path,
    task_id: str,
    trial_index: int,
    captured: dict[str, int] | None,
    *,
    extra: dict | None = None,
) -> Path:
    dest = output_dir / "trials" / task_id / str(trial_index)
    dest.mkdir(parents=True)
    payload: dict = {"cost_usd": 0.01}
    if captured is not None:
        payload["captured_service_logs"] = captured
    if extra:
        payload.update(extra)
    path = dest / "metrics.yaml"
    with path.open("w") as f:
        yaml.safe_dump(payload, f)
    return path


def _write_shared_stack_capture(
    output_dir: Path,
    captured: dict[str, int],
    *,
    capture_reason: str = "materialise_error",
) -> None:
    dest = run_services_dir(output_dir)
    dest.mkdir(parents=True)
    write_capture_manifest(dest, tail=500, captured=captured, capture_reason=capture_reason)


# ---------------------------------------------------------------------------
# Clean / empty run
# ---------------------------------------------------------------------------


def test_empty_output_dir_yields_zero_envelope(tmp_path: Path) -> None:
    """A run that captured nothing rolls up to the explicit zero envelope."""
    rollup = collect_service_log_captures(tmp_path)
    assert rollup.captures == 0
    assert rollup.total_bytes == 0
    assert rollup.per_service_bytes == {}
    assert rollup.entries == []


# ---------------------------------------------------------------------------
# Single-surface
# ---------------------------------------------------------------------------


def test_provision_failure_surface_only(tmp_path: Path) -> None:
    _write_provision_capture(tmp_path, "task-a", 0, {"db": 4096, "runner": 512})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    entry = rollup.entries[0]
    assert entry.source is ServiceLogCaptureSource.PROVISION_FAILURE
    assert entry.task_id == "task-a"
    assert entry.trial_index == 0
    assert entry.capture_reason == "provision_error"
    assert entry.services == {"db": 4096, "runner": 512}
    assert entry.total_bytes == 4608
    assert rollup.per_service_bytes == {"db": 4096, "runner": 512}
    assert rollup.total_bytes == 4608


def test_trial_body_surface_only(tmp_path: Path) -> None:
    _write_trial_body_metrics(tmp_path, "task-b", 1, {"db": 1024})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    entry = rollup.entries[0]
    assert entry.source is ServiceLogCaptureSource.TRIAL_BODY
    assert entry.task_id == "task-b"
    assert entry.trial_index == 1
    assert entry.capture_reason is None
    assert entry.services == {"db": 1024}
    assert entry.total_bytes == 1024


def test_shared_stack_surface_only(tmp_path: Path) -> None:
    _write_shared_stack_capture(tmp_path, {"api": 3584})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    entry = rollup.entries[0]
    assert entry.source is ServiceLogCaptureSource.SHARED_STACK_MATERIALISE
    assert entry.task_id is None
    assert entry.trial_index is None
    assert entry.capture_reason == "materialise_error"
    assert entry.services == {"api": 3584}
    assert entry.total_bytes == 3584


# ---------------------------------------------------------------------------
# All three surfaces + aggregation
# ---------------------------------------------------------------------------


def test_mixed_surfaces_aggregate_and_sort(tmp_path: Path) -> None:
    """All three surfaces present; ``db`` recurs across two entries so
    ``per_service_bytes`` sums it, and entries land in deterministic order."""
    _write_provision_capture(tmp_path, "task-a", 0, {"db": 4096, "runner": 512})
    _write_trial_body_metrics(tmp_path, "task-b", 1, {"db": 1024})
    _write_shared_stack_capture(tmp_path, {"api": 3584})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 3
    assert rollup.total_bytes == 9216
    assert rollup.per_service_bytes == {"db": 5120, "runner": 512, "api": 3584}

    # Sorted by (task_id or "", trial_index or -1, source): run-level first.
    assert [e.source for e in rollup.entries] == [
        ServiceLogCaptureSource.SHARED_STACK_MATERIALISE,
        ServiceLogCaptureSource.PROVISION_FAILURE,
        ServiceLogCaptureSource.TRIAL_BODY,
    ]


# ---------------------------------------------------------------------------
# Fail-safe contract
# ---------------------------------------------------------------------------


def test_malformed_capture_yaml_skipped_others_counted(tmp_path: Path) -> None:
    """A corrupt ``_capture.yaml`` is skipped; a valid sibling is still counted."""
    _write_provision_capture(tmp_path, "task-good", 0, {"db": 100})
    bad_dir = tmp_path / "trials" / "task-bad" / "0" / "services"
    bad_dir.mkdir(parents=True)
    (bad_dir / "_capture.yaml").write_text("{ this is not: valid: yaml ]")

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    assert rollup.entries[0].task_id == "task-good"


def test_malformed_metrics_yaml_skipped(tmp_path: Path) -> None:
    dest = tmp_path / "trials" / "task-bad" / "0"
    dest.mkdir(parents=True)
    (dest / "metrics.yaml").write_text(": : not valid : :")

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 0


def test_non_int_trial_index_dir_skipped(tmp_path: Path) -> None:
    _write_provision_capture(tmp_path, "task-a", 0, {"db": 100})
    weird = tmp_path / "trials" / "task-a" / "latest" / "services"
    weird.mkdir(parents=True)
    write_capture_manifest(weird, tail=1, captured={"db": 999})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    assert rollup.entries[0].trial_index == 0


def test_metrics_without_captured_key_yields_no_entry(tmp_path: Path) -> None:
    _write_trial_body_metrics(tmp_path, "task-a", 0, captured=None, extra={"turns": 5})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 0


def test_metrics_with_empty_captured_map_yields_no_entry(tmp_path: Path) -> None:
    """An empty ``captured_service_logs`` map is falsy — no capture happened."""
    _write_trial_body_metrics(tmp_path, "task-a", 0, captured={})

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 0


def test_manifest_with_non_int_bytes_skips_that_service(tmp_path: Path) -> None:
    """A mistyped byte count drops that service but keeps the well-formed one."""
    dest = tmp_path / "trials" / "task-a" / "0" / "services"
    dest.mkdir(parents=True)
    with (dest / "_capture.yaml").open("w") as f:
        yaml.safe_dump(
            {
                "tail": 500,
                "capture_reason": "provision_error",
                "services": {"db": {"bytes": 4096}, "runner": {"bytes": "lots"}},
            },
            f,
        )

    rollup = collect_service_log_captures(tmp_path)

    assert rollup.captures == 1
    assert rollup.entries[0].services == {"db": 4096}
