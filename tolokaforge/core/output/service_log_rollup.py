"""Roll the run's on-disk captured-service-log bundles into one aggregate.

Scans the three capture surfaces a run leaves behind and folds them into a
single :class:`CapturedServiceLogsRollup` for ``aggregate.json``:

* Surface A — per-trial provision / reset-recipe failure
  (``trials/<task>/<idx>/services/_capture.yaml``).
* Surface B — per-trial trial-body / graded-red failure
  (``trials/<task>/<idx>/metrics.yaml`` → ``captured_service_logs``).
* Surface C — run-level shared-stack materialise failure
  (``<output_dir>/services/_capture.yaml``).

Best-effort diagnostics reader over possibly-partial disk state: a missing
dir/file, malformed YAML, a non-mapping payload, a missing/wrong-typed key, or
a non-int trial-index dir is logged and that surface is skipped — the collector
never raises, so report generation is never broken by a corrupt artifact.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.compose_materialisation import run_services_dir
from tolokaforge.core.output.aggregate_models import (
    CapturedServiceLogsRollup,
    ServiceLogCaptureEntry,
    ServiceLogCaptureSource,
)

__all__ = ["collect_service_log_captures"]

logger = logging.getLogger(__name__)


def collect_service_log_captures(output_dir: Path) -> CapturedServiceLogsRollup:
    """Scan ``output_dir`` for captured-service-log bundles and roll them up.

    Always returns a rollup: a run that captured nothing yields the zero
    envelope (``captures=0``, empty maps/lists).
    """
    entries: list[ServiceLogCaptureEntry] = []
    entries.extend(_provision_failure_entries(output_dir))
    entries.extend(_trial_body_entries(output_dir))
    shared = _shared_stack_entry(output_dir)
    if shared is not None:
        entries.append(shared)

    entries.sort(
        key=lambda e: (
            e.task_id or "",
            e.trial_index if e.trial_index is not None else -1,
            e.source.value,
        )
    )

    per_service_bytes: dict[str, int] = {}
    total_bytes = 0
    for entry in entries:
        total_bytes += entry.total_bytes
        for name, count in entry.services.items():
            per_service_bytes[name] = per_service_bytes.get(name, 0) + count

    return CapturedServiceLogsRollup(
        captures=len(entries),
        total_bytes=total_bytes,
        per_service_bytes=per_service_bytes,
        entries=entries,
    )


def _provision_failure_entries(output_dir: Path) -> list[ServiceLogCaptureEntry]:
    entries: list[ServiceLogCaptureEntry] = []
    for path in sorted((output_dir / "trials").glob("*/*/services/_capture.yaml")):
        idx_dir = path.parent.parent
        trial_index = _parse_trial_index(idx_dir.name, path)
        if trial_index is None:
            continue
        entry = _manifest_entry(
            path,
            source=ServiceLogCaptureSource.PROVISION_FAILURE,
            task_id=idx_dir.parent.name,
            trial_index=trial_index,
        )
        if entry is not None:
            entries.append(entry)
    return entries


def _trial_body_entries(output_dir: Path) -> list[ServiceLogCaptureEntry]:
    entries: list[ServiceLogCaptureEntry] = []
    for path in sorted((output_dir / "trials").glob("*/*/metrics.yaml")):
        metrics = _load_yaml_mapping(path)
        if metrics is None:
            continue
        raw = metrics.get("captured_service_logs")
        if not raw:
            continue
        services = _parse_flat_services(raw, path)
        if not services:
            continue
        idx_dir = path.parent
        trial_index = _parse_trial_index(idx_dir.name, path)
        if trial_index is None:
            continue
        entries.append(
            ServiceLogCaptureEntry(
                task_id=idx_dir.parent.name,
                trial_index=trial_index,
                source=ServiceLogCaptureSource.TRIAL_BODY,
                capture_reason=None,
                total_bytes=sum(services.values()),
                services=services,
            )
        )
    return entries


def _shared_stack_entry(output_dir: Path) -> ServiceLogCaptureEntry | None:
    path = run_services_dir(output_dir) / "_capture.yaml"
    if not path.is_file():
        return None
    return _manifest_entry(
        path,
        source=ServiceLogCaptureSource.SHARED_STACK_MATERIALISE,
        task_id=None,
        trial_index=None,
    )


def _manifest_entry(
    path: Path,
    *,
    source: ServiceLogCaptureSource,
    task_id: str | None,
    trial_index: int | None,
) -> ServiceLogCaptureEntry | None:
    """Build an entry from a ``_capture.yaml`` manifest (surfaces A and C)."""
    manifest = _load_yaml_mapping(path)
    if manifest is None:
        return None
    services = _parse_manifest_services(manifest, path)
    if not services:
        return None
    reason = manifest.get("capture_reason")
    return ServiceLogCaptureEntry(
        task_id=task_id,
        trial_index=trial_index,
        source=source,
        capture_reason=reason if isinstance(reason, str) else None,
        total_bytes=sum(services.values()),
        services=services,
    )


def _parse_manifest_services(manifest: Mapping[str, Any], path: Path) -> dict[str, int]:
    """Normalise a manifest ``services`` map (``{name: {bytes: N}}``) to ``{name: N}``."""
    raw = manifest.get("services")
    if not isinstance(raw, Mapping):
        logger.warning("service-log rollup: non-mapping 'services' in %s", path)
        return {}
    services: dict[str, int] = {}
    for name, spec in raw.items():
        count = spec.get("bytes") if isinstance(spec, Mapping) else None
        if not _is_byte_count(count):
            logger.warning("service-log rollup: bad byte count for %r in %s", name, path)
            continue
        services[str(name)] = count
    return services


def _parse_flat_services(raw: Any, path: Path) -> dict[str, int]:
    """Normalise a flat ``{name: bytes}`` map (surface-B ``metrics.yaml``)."""
    if not isinstance(raw, Mapping):
        logger.warning("service-log rollup: non-mapping captured_service_logs in %s", path)
        return {}
    services: dict[str, int] = {}
    for name, count in raw.items():
        if not _is_byte_count(count):
            logger.warning("service-log rollup: bad byte count for %r in %s", name, path)
            continue
        services[str(name)] = count
    return services


def _is_byte_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_trial_index(raw: str, path: Path) -> int | None:
    try:
        return int(raw)
    except ValueError:
        logger.warning("service-log rollup: non-int trial-index dir %r at %s", raw, path)
        return None


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("service-log rollup: unreadable YAML at %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("service-log rollup: non-mapping payload at %s", path)
        return None
    return data
