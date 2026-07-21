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

DEFAULT_CONFIGS_DIR = REPO_ROOT / "examples" / "open_agent_loop_coaching"

# Base arm definitions keyed by suffix. When `--judge-batch <suffix>` is
# passed the driver appends `_<suffix>` to each arm's run_config filename
# (e.g. `solo_sonnet_judge.yaml`) so two batches can share the same
# coach_configs but differ in judge model.
_ARM_TEMPLATE: dict[str, dict[str, str | None]] = {
    "solo": {
        "run_config_stem": "solo",
        "coach_config": None,
    },
    "rule_coached": {
        "run_config_stem": "rule_coached",
        "coach_config": "coach_configs/rule.yaml",
    },
    "llm_coached": {
        "run_config_stem": "llm_coached",
        "coach_config": "coach_configs/llm.yaml",
    },
}

# Hard budget rail — if cumulative total_cost_usd across arms crosses this,
# remaining arms in the batch abort. Safety margin under the $10 study cap.
_CUMULATIVE_COST_LIMIT_USD = 8.0

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


def _resolve_output_dir(configured: Path) -> Path:
    """Find the on-disk output directory the orchestrator actually used.

    Prefers the pristine configured path if it exists; falls back to the
    most-recent `<name>_YYYYMMDD_HHMMSS` sibling, which is what the
    orchestrator produces when `run_id` is auto-generated.
    """
    if configured.is_dir():
        return configured
    parent = configured.parent
    if not parent.is_dir():
        return configured
    prefix = f"{configured.name}_"
    candidates = sorted(
        (p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)),
        reverse=True,
    )
    return candidates[0] if candidates else configured


def _expand_braces(pattern: str) -> list[str]:
    """Shell-style brace expansion: `a/{x,y}/z` → `[a/x/z, a/y/z]`.

    pathlib.Path.glob does not support braces; the OTS smoke configs
    rely on them (`tasks/tau_manufacturing/{MAN-34,MAL-007,MAN-46}/task.yaml`).
    Handles one level of nesting, which is all the existing configs need.
    """
    import re

    if "{" not in pattern:
        return [pattern]
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    prefix, suffix = pattern[: m.start()], pattern[m.end() :]
    expansions: list[str] = []
    for choice in m.group(1).split(","):
        expansions.extend(_expand_braces(prefix + choice + suffix))
    return expansions


def _discover_trial_ids(config: RunConfig) -> list[str]:
    """Enumerate the trial IDs the orchestrator will produce, so we can
    pre-create sessions before the run starts.

    Reads `evaluation.projects` (canonical) with `evaluation.task_packs`
    fallback for legacy configs — the Pydantic model coerces `task_packs`
    into `projects` internally, so post-validation `projects` is where
    the actual paths live. Supports absolute `projects` paths (for OTS
    task packs living outside the repo) and shell-style brace expansion
    in `tasks_glob`.
    """
    trial_ids: list[str] = []
    project_paths = list(config.evaluation.projects) or list(config.evaluation.task_packs)
    for project in project_paths:
        project_root = REPO_ROOT / project  # abs paths on RHS override the join
        for glob_pattern in _expand_braces(config.evaluation.tasks_glob):
            for task_file in project_root.glob(glob_pattern):
                task_raw = yaml.safe_load(task_file.read_text()) or {}
                task_id = task_raw.get("task_id") or task_file.parent.name
                for trial_idx in range(config.orchestrator.repeats):
                    trial_ids.append(f"{task_id}:{trial_idx}")
    return trial_ids


def _read_arm_cost_usd(output_root: Path) -> float:
    """Read `total_cost_incl_judge_usd` from the arm's aggregate.json.

    Returns 0.0 if the file doesn't exist (arm aborted early) or the
    field is missing. The value is the sum of agent + user + judge
    costs across all trials in the arm; coach LLM spend is separate.
    """
    import json

    agg = output_root / "aggregate.json"
    if not agg.is_file():
        return 0.0
    try:
        data = json.loads(agg.read_text())
    except Exception:
        return 0.0
    return float(data.get("total_cost_incl_judge_usd") or data.get("total_cost_usd") or 0.0)


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
    # The orchestrator stamps `_<timestamp>` onto the configured output_dir,
    # so we resolve the actual on-disk root by preferring the pristine path
    # and falling back to the most-recent `<name>_*` sibling.
    output_root = _resolve_output_dir(REPO_ROOT / config.evaluation.output_dir)
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


def _resolve_arm_paths(
    configs_dir: Path, judge_batch: str | None
) -> dict[str, dict[str, Path | None]]:
    """Build the concrete file-path arm map for one batch.

    `judge_batch` picks a suffix — `sonnet` → `solo_sonnet_judge.yaml`,
    `opus` → `solo_opus_judge.yaml`, or `None` → `solo.yaml`
    (backward-compatible with the original tool_use configs).
    """
    suffix = f"_{judge_batch}_judge" if judge_batch else ""
    result: dict[str, dict[str, Path | None]] = {}
    for arm_name, template in _ARM_TEMPLATE.items():
        run_config = configs_dir / "run_configs" / f"{template['run_config_stem']}{suffix}.yaml"
        coach_config = configs_dir / template["coach_config"] if template["coach_config"] else None
        # Fallback: coach_configs may live under configs_dir OR the default
        # dir (task-agnostic — reused across experiments).
        if coach_config is not None and not coach_config.is_file():
            fallback = DEFAULT_CONFIGS_DIR / template["coach_config"]
            if fallback.is_file():
                coach_config = fallback
        result[arm_name] = {"run_config": run_config, "coach_config": coach_config}
    return result


def main(argv: list[str] | None = None) -> int:
    _silence_loggers()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--configs-dir",
        type=Path,
        default=DEFAULT_CONFIGS_DIR,
        help=(
            "Directory containing run_configs/ and coach_configs/. "
            f"Default: {DEFAULT_CONFIGS_DIR.relative_to(REPO_ROOT)}"
        ),
    )
    parser.add_argument(
        "--judge-batch",
        choices=("sonnet", "opus"),
        default=None,
        help=(
            "When set, appends `_<batch>_judge` to run-config filenames "
            "so a config dir can carry multiple batches side by side. "
            "Default: unsuffixed (works with the original tool_use configs)."
        ),
    )
    parser.add_argument(
        "--arm",
        action="append",
        choices=sorted(_ARM_TEMPLATE),
        help="Run only the given arm(s). Repeatable. Default: all three.",
    )
    args = parser.parse_args(argv)

    configs_dir: Path = args.configs_dir
    if not configs_dir.is_absolute():
        configs_dir = REPO_ROOT / configs_dir
    if not configs_dir.is_dir():
        print(f"configs-dir does not exist: {configs_dir}", file=sys.stderr)
        return 2

    arm_paths = _resolve_arm_paths(configs_dir, args.judge_batch)
    selected = args.arm if args.arm else list(_ARM_TEMPLATE)

    cumulative_cost_usd = 0.0
    started = time.time()
    for arm_name in selected:
        if cumulative_cost_usd >= _CUMULATIVE_COST_LIMIT_USD:
            print(
                f"\n! cumulative cost ${cumulative_cost_usd:.2f} ≥ hard rail "
                f"${_CUMULATIVE_COST_LIMIT_USD:.2f}. Aborting remaining arms.",
                file=sys.stderr,
            )
            break
        arm = arm_paths[arm_name]
        _run_arm(
            arm_name=arm_name,
            run_config_path=arm["run_config"],
            coach_config_path=arm["coach_config"],
        )
        # Consult the arm's own aggregate.json (the orchestrator's honest
        # accounting) for the cumulative check.
        run_config = _load_run_config(arm["run_config"])
        output_root = _resolve_output_dir(REPO_ROOT / run_config.evaluation.output_dir)
        arm_cost = _read_arm_cost_usd(output_root)
        cumulative_cost_usd += arm_cost
        print(
            f"  arm cost (from aggregate.json): ${arm_cost:.4f}  "
            f"cumulative: ${cumulative_cost_usd:.4f}"
        )

    total = time.time() - started
    print(f"\n{'═' * 60}\n▶ all arms complete in {total:.1f}s")
    print(f"▶ cumulative trial cost (from aggregates): ${cumulative_cost_usd:.4f}")
    print(
        "\nNext: run "
        "`uv run python examples/open_agent_loop_coaching/analyze_results.py "
        "--results-dir <results-root>` to compute the A/B summary."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
