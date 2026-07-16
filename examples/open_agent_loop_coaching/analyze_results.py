"""Analyze the A/B study — cross-arm summary, notable saves, notable harm.

Reads each arm's `aggregate.json` (written by tolokaforge automatically),
per-trial `grade.yaml` (per-trial verdict), and per-trial
`coach_report.yaml` (coach behaviour + cost), and produces:

* stdout table comparing arms on pass@1, avg turns, cost breakdown
* `ab_summary.yaml` at the results root with structured deltas and the
  full list of notable saves / notable harm

Usage:
  uv run python examples/open_agent_loop_coaching/analyze_results.py \\
      [--results-dir results/coaching_ab]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results" / "coaching_ab"

_ARM_ORDER = ["solo", "rule_coached", "llm_coached"]


@dataclass
class TrialRecord:
    trial_id: str
    passed: bool | None
    score: float | None
    turns: int | None
    agent_cost_usd: float
    coach_report: dict[str, Any] | None


@dataclass
class ArmSummary:
    name: str
    trials: list[TrialRecord] = field(default_factory=list)
    aggregate: dict[str, Any] | None = None

    @property
    def pass_rate(self) -> float:
        if not self.trials:
            return 0.0
        passes = sum(1 for t in self.trials if t.passed)
        return passes / len(self.trials)

    @property
    def avg_turns(self) -> float:
        turns = [t.turns for t in self.trials if t.turns is not None]
        return sum(turns) / len(turns) if turns else 0.0

    @property
    def agent_cost_usd(self) -> float:
        return sum(t.agent_cost_usd for t in self.trials)

    @property
    def coach_cost_usd(self) -> float:
        return sum((t.coach_report or {}).get("coach_cost_usd", 0.0) for t in self.trials)

    @property
    def total_cost_usd(self) -> float:
        return self.agent_cost_usd + self.coach_cost_usd

    @property
    def total_interventions(self) -> int:
        return sum((t.coach_report or {}).get("interventions_submitted", 0) for t in self.trials)


def load_arm(arm_dir: Path, arm_name: str) -> ArmSummary:
    summary = ArmSummary(name=arm_name)

    agg_path = arm_dir / "aggregate.json"
    if agg_path.is_file():
        summary.aggregate = json.loads(agg_path.read_text())

    trials_dir = arm_dir / "trials"
    if not trials_dir.is_dir():
        return summary

    for task_dir in sorted(trials_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        for idx_dir in sorted(
            task_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0
        ):
            if not idx_dir.is_dir():
                continue
            trial_id = f"{task_dir.name}:{idx_dir.name}"
            grade = _load_yaml(idx_dir / "grade.yaml")
            metrics = _load_yaml(idx_dir / "metrics.yaml")
            coach_report = _load_yaml(idx_dir / "coach_report.yaml")
            summary.trials.append(
                TrialRecord(
                    trial_id=trial_id,
                    passed=_extract_pass(grade),
                    score=_extract_score(grade),
                    turns=_extract_turns(metrics),
                    agent_cost_usd=_extract_cost(metrics),
                    coach_report=coach_report,
                )
            )
    return summary


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open() as fh:
        return yaml.safe_load(fh)


def _extract_pass(grade: dict[str, Any] | None) -> bool | None:
    if not grade:
        return None
    return grade.get("binary_pass")


def _extract_score(grade: dict[str, Any] | None) -> float | None:
    if not grade:
        return None
    return grade.get("score")


def _extract_turns(metrics: dict[str, Any] | None) -> int | None:
    if not metrics:
        return None
    return metrics.get("turns")


def _extract_cost(metrics: dict[str, Any] | None) -> float:
    if not metrics:
        return 0.0
    return float(metrics.get("cost_usd") or 0.0)


def find_notable(
    solo: ArmSummary, coached: ArmSummary
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(saves, harm) — trials where coached differs from solo in outcome."""
    solo_by_id = {t.trial_id: t for t in solo.trials}
    saves: list[dict[str, Any]] = []
    harm: list[dict[str, Any]] = []
    for c in coached.trials:
        s = solo_by_id.get(c.trial_id)
        if s is None:
            continue
        if s.passed is None or c.passed is None or s.passed == c.passed:
            continue
        entry = {
            "trial_id": c.trial_id,
            "solo": _outcome_str(s),
            "coached": _outcome_str(c),
            "coach_interventions": [
                {
                    "kind": t.get("intervention_kind"),
                    "at_seq": t.get("at_seq"),
                    "reason": t.get("reason"),
                }
                for t in (c.coach_report or {}).get("trigger_events", [])
            ],
            "coach_cost_usd": (c.coach_report or {}).get("coach_cost_usd", 0.0),
        }
        (saves if c.passed and not s.passed else harm).append(entry)
    return saves, harm


def _outcome_str(t: TrialRecord) -> str:
    verdict = "passed" if t.passed else "failed"
    return f"{verdict} (turns={t.turns}, cost=${t.agent_cost_usd:.4f})"


def print_table(arms: list[ArmSummary]) -> None:
    print(f"\n{'═' * 88}")
    print("A/B Coaching Study — arm comparison")
    print("═" * 88)
    header = f"{'arm':<18}{'trials':>8}{'pass rate':>12}{'avg turns':>12}{'agent $':>12}{'coach $':>12}{'total $':>12}"
    print(header)
    print("-" * 88)
    baseline = next((a for a in arms if a.name == "solo"), None)
    for a in arms:
        delta = ""
        if baseline is not None and a is not baseline:
            delta = f"  Δpass {a.pass_rate - baseline.pass_rate:+.2f}"
        print(
            f"{a.name:<18}"
            f"{len(a.trials):>8}"
            f"{a.pass_rate:>12.2f}"
            f"{a.avg_turns:>12.1f}"
            f"{a.agent_cost_usd:>12.4f}"
            f"{a.coach_cost_usd:>12.4f}"
            f"{a.total_cost_usd:>12.4f}" + delta
        )
    print("═" * 88)


def print_notable(kind: str, entries: list[dict[str, Any]], baseline: str, arm_name: str) -> None:
    if not entries:
        return
    marker = "✓ SAVES" if kind == "save" else "✗ HARM"
    print(f"\n{marker} — {arm_name} vs {baseline} ({len(entries)} trial(s))")
    for e in entries:
        print(f"  trial={e['trial_id']}")
        print(f"    solo:    {e['solo']}")
        print(f"    coached: {e['coached']}  (coach cost ${e['coach_cost_usd']:.4f})")
        if e["coach_interventions"]:
            print(f"    triggers ({len(e['coach_interventions'])}):")
            for t in e["coach_interventions"][:3]:
                print(f"      at_seq={t['at_seq']}  {t['reason']}")


def build_summary_yaml(arms: dict[str, ArmSummary]) -> dict[str, Any]:
    baseline = arms.get("solo")
    result: dict[str, Any] = {"arms": {}, "notable_saves": [], "notable_harm": []}
    for name, arm in arms.items():
        arm_block: dict[str, Any] = {
            "trials": len(arm.trials),
            "pass_rate": round(arm.pass_rate, 4),
            "avg_turns": round(arm.avg_turns, 2),
            "agent_cost_usd": round(arm.agent_cost_usd, 6),
            "coach_cost_usd": round(arm.coach_cost_usd, 6),
            "total_cost_usd": round(arm.total_cost_usd, 6),
            "coach_interventions_total": arm.total_interventions,
        }
        if baseline is not None and name != "solo":
            arm_block["delta_pass_rate_vs_solo"] = round(arm.pass_rate - baseline.pass_rate, 4)
            arm_block["delta_total_cost_usd_vs_solo"] = round(
                arm.total_cost_usd - baseline.total_cost_usd, 6
            )
        result["arms"][name] = arm_block

    if baseline is not None:
        for name, arm in arms.items():
            if name == "solo":
                continue
            saves, harm = find_notable(baseline, arm)
            for entry in saves:
                entry["arm"] = name
                result["notable_saves"].append(entry)
            for entry in harm:
                entry["arm"] = name
                result["notable_harm"].append(entry)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
        help=f"Root of the A/B run outputs (default: {DEFAULT_RESULTS_ROOT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write ab_summary.yaml here (default: <results-dir>/ab_summary.yaml).",
    )
    args = parser.parse_args(argv)

    results_dir: Path = args.results_dir
    if not results_dir.is_dir():
        print(f"results dir not found: {results_dir}", file=sys.stderr)
        return 2

    arms: dict[str, ArmSummary] = {}
    for arm_name in _ARM_ORDER:
        arm_dir = results_dir / arm_name
        if arm_dir.is_dir():
            arms[arm_name] = load_arm(arm_dir, arm_name)

    if not arms:
        print(f"no arms found under {results_dir}", file=sys.stderr)
        return 2

    print_table(list(arms.values()))

    baseline = arms.get("solo")
    if baseline is not None:
        for name, arm in arms.items():
            if name == "solo":
                continue
            saves, harm = find_notable(baseline, arm)
            print_notable("save", saves, baseline.name, name)
            print_notable("harm", harm, baseline.name, name)

    summary = build_summary_yaml(arms)
    output_path = args.output or (results_dir / "ab_summary.yaml")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as fh:
        yaml.safe_dump(summary, fh, sort_keys=False)
    print(f"\nsummary written → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
