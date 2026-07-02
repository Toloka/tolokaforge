"""Static (no-API) guard for the wire_probes smoke task-pack.

Asserts every task under ``tests/data/tasks/wire_probes`` is NON-SCORING
(combine-only grading, no oracle / scoring component sections) and is
structurally coherent (task_id matches its dir, domain + grading refs
present). Runs statically as a canonical contract check (no Docker, no provider calls).

This is the STATIC half of the wire-probe smoke set; the live behavioural
half (per-model orchestrator run) is a planned follow-up
(``tests/integration/test_wire_probe_smoke.py``, not yet in this pack). The
convertible atomic probes live separately as capability tests in
``tests/integration/llm/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical

_PACK = Path(__file__).resolve().parents[1] / "data" / "tasks" / "wire_probes" / "dataset"
_TASKS = sorted((_PACK / "testcases").glob("*/task.yaml"))
_SCORING_KEYS = {"state_checks", "transcript_rules", "llm_judge", "custom_checks"}


def test_pack_present() -> None:
    assert _TASKS, f"no wire_probes tasks found under {_PACK}"
    names = {p.parent.name for p in _TASKS}
    assert any(n.startswith("record_") for n in names), "no record_* (atomic) tasks"
    assert any(n.startswith("cascade_") for n in names), "no cascade_* (multi-turn) tasks"


@pytest.mark.parametrize("task_yaml", _TASKS, ids=lambda p: p.parent.name)
def test_task_structural(task_yaml: Path) -> None:
    d = yaml.safe_load(task_yaml.read_text())
    assert d.get("task_id") == task_yaml.parent.name, (
        f"task_id {d.get('task_id')!r} must match dir {task_yaml.parent.name!r}"
    )
    assert d.get("domain"), f"{task_yaml.parent.name}: missing domain ref"
    assert d.get("grading"), f"{task_yaml.parent.name}: missing grading ref"


@pytest.mark.parametrize("task_yaml", _TASKS, ids=lambda p: p.parent.name)
def test_grading_non_scoring(task_yaml: Path) -> None:
    d = yaml.safe_load(task_yaml.read_text())
    grading = task_yaml.parent / (d.get("grading") or "grading.yaml")
    g = yaml.safe_load(grading.read_text()) or {}
    present = _SCORING_KEYS & set(g)
    assert not present, (
        f"{task_yaml.parent.name}: NON-SCORING contract violated - found scoring "
        f"section(s) {sorted(present)} in {grading.name}"
    )
    assert "combine" in g, f"{task_yaml.parent.name}: expected combine-only grading"
