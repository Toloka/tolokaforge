"""Open Agent Loop example — run tool-use trials with a live LLM copilot attached.

Wires the pieces the `feat/open-agent-loop` workstream shipped:

* Configures a run with ``open_agent_loop.enabled: true`` — see
  ``examples/open_agent_loop/run_config.yaml``.
* Pre-creates each trial's :class:`InProcessTrialSession` via
  ``orchestrator.sessions.get_or_create`` so a copilot can attach before
  the trial actually starts.
* Spawns an :class:`LLMIntervener` on a background thread per expected
  trial — it iterates events as the trial runs and proposes interventions
  (drafter uses ``ANTHROPIC_API_KEY`` if available; otherwise the built-in
  deterministic heuristic).
* Runs the orchestrator on the main thread (blocks until every trial
  finishes and every session is closed).
* Waits for the copilots to drain, prints a summary of every intervention
  it proposed and the ack outcome recorded in the trace.

The intervener package lives at ``tools/intervener/`` — see its README for
the participant contract. The gate itself is documented in
``docs/OPEN_AGENT_LOOP.md``.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

# The intervener is a workspace peer; when this script is run via
# ``uv run python`` from the repo root the workspace install makes it
# importable directly.
from intervener.participants import LLMIntervener

from tolokaforge.core.models import RunConfig
from tolokaforge.core.orchestrator import Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "examples" / "open_agent_loop" / "run_config.yaml"


def _load_config(config_path: Path) -> RunConfig:
    with config_path.open() as fh:
        raw = yaml.safe_load(fh)
    return RunConfig.model_validate(raw)


def _discover_trial_ids(config: RunConfig) -> list[str]:
    """Best-effort enumeration of the trial_ids this run will produce.

    Walks the configured task packs, applies the tasks glob, and builds
    ``"{task_id}:{trial_index}"`` for every ``trials`` combination the
    orchestrator would spawn. Kept intentionally simple: any task the
    config would run gets its session pre-created.
    """
    trial_ids: list[str] = []
    tasks_glob = config.evaluation.tasks_glob
    repeats = config.orchestrator.repeats
    for task_pack in config.evaluation.task_packs:
        task_root = REPO_ROOT / task_pack
        for task_file in task_root.glob(tasks_glob):
            with task_file.open() as fh:
                task_raw = yaml.safe_load(fh)
            task_id = task_raw.get("task_id") or task_file.parent.name
            for trial_idx in range(repeats):
                trial_ids.append(f"{task_id}:{trial_idx}")
    return trial_ids


def _spawn_copilot_thread(orchestrator: Orchestrator, trial_id: str) -> threading.Thread:
    """Pre-create the session for ``trial_id`` and start a copilot on it.

    Pre-creating the session before the orchestrator's observer provider
    tries to is safe — :meth:`SessionRegistry.get_or_create` is idempotent
    and threadsafe. The observer provider will hand back the same session,
    and the LoopObserver will publish into it while the copilot's own
    thread iterates events.
    """
    session = orchestrator.sessions.get_or_create(trial_id)
    copilot = LLMIntervener(
        participant_id=f"copilot:{trial_id}",
        auto_inject=False,  # log suggestions in the trace, don't inject into the trial
    )

    thread = threading.Thread(
        target=copilot.run,
        args=(session,),
        name=f"copilot-{trial_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _print_summary(config: RunConfig, trial_ids: list[str]) -> None:
    output_root = REPO_ROOT / config.evaluation.output_dir
    print(f"\nOpen-agent-loop trace summaries (results in {output_root}):")
    for trial_id in trial_ids:
        task_id, idx_s = trial_id.rsplit(":", 1)
        trace_path = output_root / "trials" / task_id / idx_s / "open_agent_loop.yaml"
        if not trace_path.exists():
            print(f"  {trial_id}: no trace (trial may not have run)")
            continue
        with trace_path.open() as fh:
            trace = yaml.safe_load(fh)
        events = trace.get("events") or []
        interventions = trace.get("interventions") or []
        outcomes = _tally_outcomes(interventions)
        print(
            f"  {trial_id}: {len(events)} events, "
            f"{len(interventions)} interventions (outcomes: {outcomes})"
        )


def _tally_outcomes(interventions: list[dict[str, Any]]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for rec in interventions:
        outcome = rec.get("ack_outcome", "unknown")
        tally[outcome] = tally.get(outcome, 0) + 1
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Run config YAML (default: {DEFAULT_CONFIG.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    config = _load_config(args.config)
    if config.open_agent_loop is None or not config.open_agent_loop.enabled:
        print(
            "This example expects `open_agent_loop.enabled: true` in the run "
            "config. Falling back to sealed mode would run the trials but skip "
            "the whole point of the demo.",
            file=sys.stderr,
        )
        return 2

    orchestrator = Orchestrator(config=config)
    if orchestrator.sessions is None:
        print("Session registry is None — open mode did not activate. Aborting.", file=sys.stderr)
        return 3

    trial_ids = _discover_trial_ids(config)
    print(f"Attaching copilot to {len(trial_ids)} trial session(s): {trial_ids}")
    copilot_threads = [_spawn_copilot_thread(orchestrator, tid) for tid in trial_ids]

    print("Running orchestrator (this makes real LLM calls; costs real tokens)…")
    orchestrator.run()

    print("Waiting for copilot threads to drain…")
    for thread in copilot_threads:
        thread.join(timeout=30.0)
        if thread.is_alive():
            print(f"  {thread.name}: still running after 30s; abandoning", file=sys.stderr)

    _print_summary(config, trial_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
