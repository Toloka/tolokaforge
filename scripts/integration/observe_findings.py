#!/usr/bin/env python
"""Deterministic aggregated findings for the model auto-integration observe stage.

Reads the observe artifact (the capability junit reports plus the non-scoring
wire-probe trajectories) and emits one structured ``findings.json`` that surfaces
the error signals the graded metrics are blind to, as RATES over K repeats so the
next step can turn them into a capability certificate and a policy-fix target.

Why this exists: the wire-probe pack is NON-SCORING, so ``grade.binary_pass``,
``aggregate.success_rate``, ``failure_attribution.json`` and even
``metrics.tool_success_rate`` all report "success" even when a tool REJECTED the
model's arguments with a schema-validation error (the tool still "responded", it
just responded with an error message). The only faithful signal is the raw
tool-result content in each trajectory. This module extracts that
deterministically (no LLM) so the next-step verification agent consumes a small,
curated structure instead of grepping every raw trajectory.

Deterministic facts only: ``mis_shapes[].hint`` is a descriptive shape fact, and
the capability ``band`` is a rate threshold, never a formatting-vs-genuine verdict
(that judgment is the verification agent's job).

Seed of the shared failure-analysis facts emitter; kept as CI glue so the observe
workflow can call it before that lands.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 2

# Capability band thresholds over the K repeats. A probe that passes >= 90% of the
# time is a required candidate; <= 10% is a known-unsupported candidate; anything
# in between is flaky and must go to a human rather than be auto-certified.
BAND_REQUIRED = 0.9
BAND_UNSUPPORTED = 0.1

# The line the engine writes into a tool-result message when the model's tool-call
# arguments fail the tool's schema validation.
_TOOL_ERROR_RE = re.compile(r"Error executing tool ([A-Za-z0-9_.-]+):\s*(.*)", re.DOTALL)


def _band(pass_rate: float) -> str:
    if pass_rate >= BAND_REQUIRED:
        return "required_candidate"
    if pass_rate <= BAND_UNSUPPORTED:
        return "known_unsupported_candidate"
    return "flaky_needs_human"


def _capability_findings(capability_dir: Path, single_report: Path) -> dict[str, Any]:
    """Aggregate the capability junit reports across K repeats into per-probe rates.

    Reads every ``*.xml`` under ``capability_dir`` (one per repeat), falling back to
    a single ``single_report`` file for a K=1 run. Skipped cases are not counted as
    runs. Each probe gets ``passed/runs`` and a reliability ``band``.
    """
    files = sorted(glob.glob(str(capability_dir / "*.xml")))
    if not files and single_report.exists():
        files = [str(single_report)]
    if not files:
        return {
            "report_present": False,
            "probes": 0,
            "runs_per_probe": 0,
            "bands": {},
            "per_probe": [],
        }

    passed: Counter = Counter()
    runs: Counter = Counter()
    for report in files:
        root = ET.parse(report).getroot()
        for case in root.iter("testcase"):
            name = case.get("name", "")
            if case.find("skipped") is not None:
                continue
            runs[name] += 1
            if case.find("failure") is None and case.find("error") is None:
                passed[name] += 1

    per_probe: list[dict[str, Any]] = []
    bands: Counter = Counter()
    for name in sorted(runs):
        n_runs = runs[name]
        n_pass = passed[name]
        rate = n_pass / n_runs if n_runs else 0.0
        band = _band(rate)
        bands[band] += 1
        per_probe.append(
            {
                "probe": name,
                "passed": n_pass,
                "runs": n_runs,
                "pass_rate": round(rate, 3),
                "band": band,
            }
        )

    return {
        "report_present": True,
        "probes": len(per_probe),
        "runs_per_probe": max((p["runs"] for p in per_probe), default=0),
        "bands": dict(bands),
        "per_probe": per_probe,
    }


def _classify_shape(arguments: Any) -> str:
    """Descriptive (NOT verdict) shape hint for a rejected tool-call's arguments.

    Deterministic facts only. Whether a shape is preset-fixable formatting or a
    genuine model error is the verification agent's judgment, not this module's.
    """
    if arguments in ({}, None, ""):
        return "empty_args"

    def _walk(value: Any) -> str | None:
        if isinstance(value, dict):
            # A sole-key ``{item: X}`` wrapper is the XML-repeated-element artifact.
            if set(value.keys()) == {"item"}:
                return "item_wrap"
            for sub in value.values():
                hit = _walk(sub)
                if hit:
                    return hit
        elif isinstance(value, list):
            for sub in value:
                hit = _walk(sub)
                if hit:
                    return hit
        elif isinstance(value, str):
            if value.strip()[:1] in ("{", "["):
                return "stringified_json"
        return None

    return _walk(arguments) or "other"


def _excerpt(obj: Any, limit: int = 300) -> str:
    try:
        text = yaml.safe_dump(obj, default_flow_style=True, sort_keys=False).strip()
    except (yaml.YAMLError, TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _wire_findings(trials_root: Path) -> dict[str, Any]:
    traj_paths = sorted(glob.glob(str(trials_root / "*" / "*" / "trajectory.yaml")))
    tasks: set[str] = set()
    task_trials: Counter = Counter()
    task_rej_trials: Counter = Counter()
    tool_call_count = 0
    rej_by_task: Counter = Counter()
    rej_by_tool: Counter = Counter()
    messages: set[str] = set()
    mis_shapes: dict[tuple[str, str], dict[str, Any]] = {}
    term: Counter = Counter()
    status_error = 0

    for path in traj_paths:
        try:
            with open(path) as handle:
                traj = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(traj, dict):
            continue
        task = traj.get("task_id") or Path(path).parent.parent.name
        tasks.add(task)
        task_trials[task] += 1
        if traj.get("termination_reason"):
            term[traj["termination_reason"]] += 1
        if traj.get("status") == "error":
            status_error += 1

        messages_list = traj.get("messages") or []
        call_args: dict[Any, tuple[Any, Any]] = {}
        for message in messages_list:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    tool_call_count += 1
                    call_args[call.get("id")] = (call.get("name"), call.get("arguments"))

        trial_rejected = False
        for message in messages_list:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            match = _TOOL_ERROR_RE.search(content)
            if not match:
                continue
            trial_rejected = True
            tool = match.group(1)
            rej_by_task[task] += 1
            rej_by_tool[tool] += 1
            messages.add(content.strip().splitlines()[0][:200])
            _name, args = call_args.get(message.get("tool_call_id"), (tool, None))
            key = (task, tool)
            if key not in mis_shapes:
                mis_shapes[key] = {
                    "task": task,
                    "tool": tool,
                    "hint": _classify_shape(args),
                    "arguments_excerpt": _excerpt(args),
                }
        if trial_rejected:
            task_rej_trials[task] += 1

    trials = len(traj_paths)
    rejecting_trials = sum(task_rej_trials.values())
    by_task_trial_rate = {
        task: round(task_rej_trials[task] / task_trials[task], 3)
        for task in sorted(task_rej_trials)
        if task_trials[task]
    }
    return {
        "trials": trials,
        "tasks": len(tasks),
        "reps_max": max(task_trials.values()) if task_trials else 0,
        "tool_call_count": tool_call_count,
        "tool_arg_rejections": {
            "total": sum(rej_by_task.values()),
            "rejecting_trials": rejecting_trials,
            "trial_rate": round(rejecting_trials / trials, 3) if trials else 0.0,
            "by_task": dict(rej_by_task.most_common()),
            "by_task_trial_rate": by_task_trial_rate,
            "by_tool": dict(rej_by_tool.most_common()),
            "messages": sorted(messages),
        },
        "mis_shapes": list(mis_shapes.values()),
        "infra": {
            "rate_limit": term.get("rate_limit", 0),
            "status_error": status_error,
            "max_turns": term.get("max_turns", 0),
            "stuck": term.get("stuck_detected", 0),
        },
    }


def build_findings(obs_dir: Path) -> dict[str, Any]:
    """Assemble the aggregated findings structure from an observe artifact directory."""
    manifest_path = obs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    wire_dirs = sorted(glob.glob(str(obs_dir / "wire_probes_*")))
    trials_root = Path(wire_dirs[-1]) / "trials" if wire_dirs else obs_dir / "trials"
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "observe",
        "candidate": manifest.get("candidate", {}),
        "preset": manifest.get("preset", "default"),
        "capability": _capability_findings(
            obs_dir / "capability", obs_dir / "capability_report.xml"
        ),
        "wire": _wire_findings(trials_root),
        "notes": [
            "wire probes are non-scoring: grade.binary_pass, aggregate.success_rate,"
            " failure_attribution.json and metrics.tool_success_rate do NOT reflect"
            " tool-argument rejections; wire.tool_arg_rejections is the faithful signal.",
            "capability.per_probe[].band and wire rates are over K repeats;"
            " flaky_needs_human means neither required nor known_unsupported was confident.",
            "mis_shapes[].hint is a descriptive shape fact, not a"
            " formatting-vs-genuine verdict (that is the verification agent's job).",
        ],
    }


def render_summary(findings: dict[str, Any], run_url: str | None = None) -> str:
    """Render a short human-readable markdown summary of the findings."""
    cap = findings.get("capability", {})
    wire = findings.get("wire", {})
    rej = wire.get("tool_arg_rejections", {})
    candidate = (findings.get("candidate") or {}).get("name", "?")
    preset = findings.get("preset", "default")
    bands = cap.get("bands", {})

    lines = [f"### Auto-integration observe: `{candidate}` on the `{preset}` preset", ""]
    lines.append(
        f"- Capability ({cap.get('probes', 0)} probes x {cap.get('runs_per_probe', 0)} reps): "
        f"{bands.get('required_candidate', 0)} required-candidate, "
        f"{bands.get('known_unsupported_candidate', 0)} known-unsupported-candidate, "
        f"**{bands.get('flaky_needs_human', 0)} flaky (needs human)**."
    )
    for probe in cap.get("per_probe", []):
        if probe["band"] != "required_candidate":
            lines.append(
                f"  - `{probe['probe']}`: {probe['passed']}/{probe['runs']} ({probe['band']})"
            )
    lines.append(
        f"- Wire ({wire.get('trials', 0)} trials, {wire.get('tool_call_count', 0)} tool calls): "
        f"**{rej.get('rejecting_trials', 0)}/{wire.get('trials', 0)} trials with a tool-arg rejection** "
        f"(rate {rej.get('trial_rate', 0)})."
    )
    for shape in wire.get("mis_shapes", []):
        task = shape["task"]
        rate = rej.get("by_task_trial_rate", {}).get(task)
        rate_str = f", {rate} of trials" if rate is not None else ""
        lines.append(f"  - `{task}` / `{shape['tool']}`: {shape['hint']}{rate_str}")
    infra = wire.get("infra", {})
    lines.append(
        f"- Infra: rate_limit={infra.get('rate_limit', 0)}, "
        f"max_turns={infra.get('max_turns', 0)}, stuck={infra.get('stuck', 0)}."
    )
    lines.append("")
    tail = "Full artifact (capability reports + trajectories + `findings.json`) for the next step."
    if run_url:
        tail += f" [Run]({run_url})"
    lines.append(tail)
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit deterministic observe-stage findings.json from an observation dir."
    )
    parser.add_argument("obs_dir", help="the observation artifact directory")
    parser.add_argument(
        "--out", default=None, help="findings JSON output path (default: <obs_dir>/findings.json)"
    )
    parser.add_argument("--summary-out", default=None, help="optional markdown summary output path")
    parser.add_argument("--run-url", default=None, help="workflow run URL to link in the summary")
    args = parser.parse_args()

    obs_dir = Path(args.obs_dir)
    findings = build_findings(obs_dir)
    out_path = Path(args.out) if args.out else obs_dir / "findings.json"
    out_path.write_text(json.dumps(findings, indent=2) + "\n")
    if args.summary_out:
        Path(args.summary_out).write_text(render_summary(findings, args.run_url))

    cap = findings["capability"]
    rej = findings["wire"]["tool_arg_rejections"]
    bands = cap.get("bands", {})
    print(
        f"findings: capability {cap.get('probes', 0)} probes x {cap.get('runs_per_probe', 0)} reps "
        f"({bands.get('flaky_needs_human', 0)} flaky); wire {rej.get('rejecting_trials', 0)}"
        f"/{findings['wire'].get('trials', 0)} trials rejected; wrote {out_path}"
    )


if __name__ == "__main__":
    main()
