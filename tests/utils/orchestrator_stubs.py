"""What a stub :class:`Orchestrator` publishes, and leaves on disk, after a run.

The CLI reads ``Orchestrator.grading_completeness`` directly after a run — no
``getattr`` default, because a default would let an orchestrator that never
computed completeness report a complete run. Every module that monkeypatches
``Orchestrator`` therefore has to satisfy that read, and does so through here so
the shape lives in one place rather than in ten copies.
"""

from __future__ import annotations

import json
from pathlib import Path

from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.orchestrator import GradingCompleteness


def complete_run(total_attempts: int = 0) -> GradingCompleteness:
    """A run that produced a verdict for every attempt it made.

    The default of zero attempts is what a stub whose ``run()`` only returns a
    path honestly ran. A test asserting about the gate itself passes the count
    it means.
    """
    return GradingCompleteness(total_attempts=total_attempts, ungradeable_trial_ids=())


def fidelity_clean_run_dir(run_dir: Path) -> Path:
    """A run directory whose numbers all close, created and returned.

    ``tolokaforge run`` checks measurement fidelity over the bundle it wrote
    before it applies any completion gate, so a stub that returns a bare
    directory trips the fidelity gate instead of whatever the test is about. A
    real run always writes these two artifacts; a stub standing in for one has
    to as well.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "aggregate.json").write_text(
        json.dumps(
            {
                "total_trials": 0,
                "measured_trials": 0,
                "scored_trials": 0,
                "infrastructure_aborts": {reason.value: 0 for reason in EXCLUDED_TYPED_REASONS},
                "harness_errors": 0,
                "ungradeable": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "per_task_metrics.json").write_text("[]", encoding="utf-8")
    return run_dir
