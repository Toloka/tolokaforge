"""Supplying a trace-check constraint block the way an operator supplies one.

Written to a file and read back through the production loader, never
hand-constructed: the path the loader records is what every one of its rejection
messages is judged on, so a fixture that skipped it would test a value the command
never builds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.grading.trace_replay import TraceChecksOverride, load_trace_checks_override


def override_file(directory: Path, block: Any) -> TraceChecksOverride:
    """Write *block* as ``<directory>/constraints.yaml`` and load it back."""
    path = Path(directory) / "constraints.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(block, sort_keys=False), encoding="utf-8")
    return load_trace_checks_override(path)
