"""Coaching A/B benchmark study — solo vs rule-coached vs LLM-coached.

Runs the ticket-resolution task (`tool_use_public_example_01`) under
three configurations back-to-back:

* `solo` — sealed baseline.
* `rule_coached` — deterministic event-pattern coach (no LLM cost).
* `llm_coached` — LLM analyzer + LLM suggester coach.

Same model config + seed across all three arms so any pass@k difference
is attributable to the coach.

After each arm's run, the trial-level `coach_report.yaml` sidecars are
written to disk alongside the trial's other artifacts. The A/B summary
is left for `analyze_results.py` — this driver just runs the trials.

Usage (from the repo root):
  scripts/with_env.sh uv run --package intervener python \\
      examples/open_agent_loop_coaching/run_ab_study.py

Arms can be selected via `--arm solo` (repeatable) to run a subset.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "examples" / "open_agent_loop_coaching"))

from coach import CoachConfig, CoachReport, build_coach  # noqa: E402

from tolokaforge.core.llm import LLMClient  # noqa: E402
from tolokaforge.core.models import Message, MessageRole, ModelConfig, RunConfig  # noqa: E402
from tolokaforge.core.orchestrator import Orchestrator  # noqa: E402

ARM_DIR = REPO_ROOT / "examples" / "open_agent_loop_coaching"

ARMS: dict[str, dict[str, str | None]] = {
    "solo": {
        "run_config": "run_configs/solo.yaml",
        "coach_config": None,
    },
    "rule_coached": {
        "run_config": "run_configs/rule_coached.yaml",
        "coach_config": "coach_configs/rule.yaml",
    },
    "llm_coached": {
        "run_config": "run_configs/llm_coached.yaml",
        "coach_config": "coach_configs/llm.yaml",
    },
}

# Sensible default for the coach's LLM. Cheap + fast.
_COACH_MODEL = ModelConfig(
    provider="openrouter",
    name="anthropic/claude-haiku-4.5",
    temperature=0.0,
    max_tokens=400,
)


def _silence_loggers() -> None:
    for name in ("orchestrator", "llm_client", "runner_client", "conductor", "tolokaforge"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _load_run_config(path: Path) -> RunConfig:
    return RunConfig.model_validate(yaml.safe_load(path.read_text()))


def _load_coach_config(path: Path) -> CoachConfig:
    return CoachConfig.model_validate(yaml.safe_load(path.read_text()))


def _discover_trial_ids(config: RunConfig) -> list[str]:
    """Enumerate the trial IDs the orchestrator will produce, so we can
    pre-create sessions before the run starts."""
    trial_ids: list[str] = []
    for task_pack in config.evaluation.task_packs:
        task_root = REPO_ROOT / task_pack
        for task_file in task_root.glob(config.evaluation.tasks_glob):
            task_raw = yaml.safe_load(task_file.read_text()) or {}
            task_id = task_raw.get("task_id") or task_file.parent.name
            for trial_idx in range(config.orchestrator.repeats):
                trial_ids.append(f"{task_id}:{trial_idx}")
    return trial_ids


def _build_llm_call(model_config: ModelConfig) -> Callable[[str, str], str]:
    """Wrap tolokaforge's LLMClient into an intervener-shaped LLMCallable.
    The coach package never imports LLMClient — the driver does that here.
    """
    client = LLMClient(model_config)

    def _call(system: str, user: str) -> str:
        result = client.generate(
            system=system,
            messages=[Message(role=MessageRole.USER, content=user)],
            max_tokens=model_config.max_tokens or 400,
        )
        return (result.text or "").strip()

    return _call


def _model_key(model_config: ModelConfig) -> str:
    """Reduce a ModelConfig name to a price-table key.

    Handles both `anthropic/claude-haiku-4.5` (OpenRouter) and bare
    `claude-haiku-4.5` (direct Anthropic) forms.
    """
    name = model_config.name.rsplit("/", 1)[-1].lower()
    if "haiku" in name:
        return "claude-haiku-4.5"
    if "sonnet" in name:
        return "claude-sonnet-4.6"
    return "default"


def _run_arm(
    arm_name: str,
    run_config_path: Path,
    coach_config_path: Path | None,
) -> None:
    """Run one arm end-to-end. Pre-creates sessions when open mode is on,
    spawns a coach per trial, waits for the run, writes coach reports."""
    print(f"\n{'═' * 60}\n▶ arm: {arm_name}\n{'═' * 60}")

    config = _load_run_config(run_config_path)
    orchestrator = Orchestrator(config=config)

    coach_config: CoachConfig | None = None
    llm_call = None
    if coach_config_path is not None:
        coach_config = _load_coach_config(coach_config_path)
        # LLM detectors/interveners need a callable. Rule coach ignores it.
        needs_llm = (
            coach_config.detector.type == "llm" or coach_config.intervener.type == "llm_suggest"
        )
        if needs_llm:
            base_call = _build_llm_call(_COACH_MODEL)
            # cost_tracker wraps this per-trial internally so we don't
            # double-count across trials
            llm_call = base_call

    coach_reports: dict[str, CoachReport] = {}
    coach_threads: list[threading.Thread] = []

    if coach_config is not None:
        if orchestrator.sessions is None:
            print(f"  ! open mode did not activate for {arm_name} — aborting arm", file=sys.stderr)
            return
        trial_ids = _discover_trial_ids(config)
        print(f"  spawning {len(trial_ids)} coach(es) — one per pre-created session")
        for trial_id in trial_ids:
            session = orchestrator.sessions.get_or_create(trial_id)
            participant, report = build_coach(
                config=coach_config,
                trial_id=trial_id,
                llm_call=llm_call,
                llm_model_key=_model_key(_COACH_MODEL),
            )
            coach_reports[trial_id] = report
            t = threading.Thread(
                target=participant.run,
                args=(session,),
                name=f"coach-{trial_id}",
                daemon=True,
            )
            t.start()
            coach_threads.append(t)

    started = time.time()
    orchestrator.run()
    elapsed = time.time() - started
    print(f"  arm complete in {elapsed:.1f}s")

    # Give coach threads a chance to drain final events + terminal ack
    for t in coach_threads:
        t.join(timeout=5.0)

    # Write coach report sidecars next to each trial's other artifacts.
    output_root = REPO_ROOT / config.evaluation.output_dir
    for trial_id, report in coach_reports.items():
        task_id, idx = trial_id.rsplit(":", 1)
        target = output_root / "trials" / task_id / idx / "coach_report.yaml"
        try:
            report.write(target)
        except Exception as exc:
            print(f"  ! failed to write coach report for {trial_id}: {exc}", file=sys.stderr)

    # Per-arm summary print
    if coach_reports:
        n_int = sum(r.interventions_submitted for r in coach_reports.values())
        total_cost = sum(r.coach_cost_usd for r in coach_reports.values())
        print(
            f"  coach: {n_int} interventions total, "
            f"${total_cost:.4f} coach LLM spend across {len(coach_reports)} trials"
        )


def main(argv: list[str] | None = None) -> int:
    _silence_loggers()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--arm",
        action="append",
        choices=sorted(ARMS),
        help="Run only the given arm(s). Repeatable. Default: all three.",
    )
    args = parser.parse_args(argv)

    selected = args.arm if args.arm else list(ARMS)

    started = time.time()
    for arm_name in selected:
        arm = ARMS[arm_name]
        _run_arm(
            arm_name=arm_name,
            run_config_path=ARM_DIR / arm["run_config"],
            coach_config_path=(ARM_DIR / arm["coach_config"]) if arm["coach_config"] else None,
        )
    total = time.time() - started
    print(f"\n{'═' * 60}\n▶ all arms complete in {total:.1f}s")
    print(
        "\nNext: run "
        "`uv run python examples/open_agent_loop_coaching/analyze_results.py` "
        "to compute the A/B summary."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
