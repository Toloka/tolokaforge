"""Engine-level run state persisted alongside the queue directory.

The file carries the engine-level inputs a worker subprocess needs to join
a run without the operator threading them through every ``tolokaforge
worker`` invocation: the canonical ``run_id`` for the run, the preset
overlay path active when ``tolokaforge prepare`` ran, and the resolved
model-data fingerprint identifying which
:mod:`tolokaforge.core.model_data` snapshot the run was scored against.

The file is small, JSON, and intentionally separate from the queue database
so that adding new engine-level fields later does not require a schema
migration. See ADR 0002 for context and ADR 0030 § "Fingerprinting for
auditability" for the fingerprint field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tolokaforge.core.model_data import (
    ModelsFingerprint,
    decode_models_fingerprint,
)

_FILENAME = "engine_run_state.json"


def write_engine_run_state(
    run_dir: Path,
    *,
    run_id: str,
    presets_file: str | None,
    models_fingerprint: ModelsFingerprint,
) -> None:
    """Write ``engine_run_state.json`` next to the run queue.

    Always writes — clearing a previously-persisted overlay requires
    re-running ``prepare`` with no ``--presets-file``, which surfaces as
    ``presets_file = None`` in the new file. ``run_id`` is required and
    persisted so workers read the same canonical identifier the
    orchestrator stamped on every ``TrialSpec``. ``models_fingerprint``
    is required and records the resolved model-data snapshot; it is
    populated by
    :func:`tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
    at each write site so a completed run identifies both the resolved
    bundle and any operator overlay folded on top.
    """
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    payload: dict[str, Any] = {
        "run_id": run_id,
        "presets_file": presets_file,
        "models_fingerprint": models_fingerprint.model_dump(mode="json"),
    }
    (Path(run_dir) / _FILENAME).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_engine_run_state(run_dir: Path) -> dict[str, Any]:
    """Read engine run state, returning an empty dict if the file is absent.

    Treats absence as "no engine-level state recorded" (e.g. the run was
    prepared before this file existed). Treats malformed JSON as a loud
    failure — silently ignoring it would let workers run with the wrong
    preset overlay, violating the loud-fail discipline.
    """
    state_path = Path(run_dir) / _FILENAME
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())


def read_persisted_presets_file(run_dir: Path) -> str | None:
    """Convenience accessor for the overlay path persisted by ``prepare``."""
    return read_engine_run_state(run_dir).get("presets_file")


def read_persisted_run_id(run_dir: Path) -> str | None:
    """Return the canonical ``run_id`` for ``run_dir`` or ``None`` if absent."""
    return read_engine_run_state(run_dir).get("run_id")


def read_persisted_models_fingerprint(run_dir: Path) -> ModelsFingerprint | None:
    """Return the fingerprint persisted for ``run_dir``, or ``None``.

    ``None`` when the state file is absent (the run has not been prepared
    yet) or the ``models_fingerprint`` field is absent (a run prepared
    before this field was introduced). A malformed field content raises
    :class:`pydantic.ValidationError` — the same loud-fail discipline
    :func:`read_engine_run_state` applies to malformed JSON.
    """
    return decode_models_fingerprint(read_engine_run_state(run_dir))
