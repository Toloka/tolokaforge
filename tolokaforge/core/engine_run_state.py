"""Engine-level run state persisted alongside the queue directory.

Today this file holds a single field — the preset overlay path active when
``tolokaforge prepare`` ran — so worker subprocesses can inherit it without
the operator threading ``--presets-file`` through every ``tolokaforge worker``
invocation.

The file is small, JSON, and intentionally separate from the queue database
so that adding new engine-level fields later does not require a schema
migration. See ADR 0002 for context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FILENAME = "engine_run_state.json"


def write_engine_run_state(run_dir: Path, *, presets_file: str | None) -> None:
    """Write ``engine_run_state.json`` next to the run queue.

    Always writes — clearing a previously-persisted overlay requires
    re-running ``prepare`` with no ``--presets-file``, which surfaces as
    ``presets_file = None`` in the new file.
    """
    payload: dict[str, Any] = {"presets_file": presets_file}
    (Path(run_dir) / _FILENAME).write_text(json.dumps(payload, indent=2) + "\n")


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
