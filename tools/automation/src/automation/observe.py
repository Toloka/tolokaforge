"""Deterministic stat collector for the model automation observe stage.

Reads the observe artifact (the capability junit reports plus the non-scoring
wire-probe trajectories) and emits one structured ``findings.json`` of RAW STATS
only: for every probe, how many of the K repeats passed; for the wire probes, how
many trials had a tool-argument rejection and the raw rejection text. It does NOT
band, classify, or judge anything - it only answers "did every test pass, or is
there a failure" and hands the raw error signal to the next step. The verification
agent is what turns these facts into a capability certificate and a policy-fix
target; keeping the analysis out of here means the deterministic layer never has to
be right about *why* something failed.

Why this exists at all: the wire-probe pack is NON-SCORING, so ``grade.binary_pass``,
``aggregate.success_rate``, ``failure_attribution.json`` and even
``metrics.tool_success_rate`` all report "success" even when a tool REJECTED the
model's arguments with a schema-validation error (the tool still "responded", it
just responded with an error message). The only faithful signal is the raw
tool-result content in each trajectory. This module surfaces that raw content
deterministically (no LLM) so the agent consumes a small curated structure instead
of grepping every trajectory - but the interpretation stays the agent's job.
"""

from __future__ import annotations

import glob
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 3

# The line the engine writes into a tool-result message when the model's tool-call
# arguments fail the tool's schema validation. DEPENDENCY PIN: this matches because
# FastMCP raises ToolError("Error executing tool <name>: ...") and the MCP wrapper
# surfaces that text as the tool OUTPUT (not behind an "Error:" prefix), so it lands
# raw in the trajectory. If a rejection ever arrives as a JSON-RPC error instead
# (reformatted to "MCP error <code>: <msg>"), this regex would miss it and the signal
# would go silently blind - revisit this pattern if that engine path changes.
_TOOL_ERROR_RE = re.compile(r"Error executing tool ([A-Za-z0-9_.-]+):\s*(.*)", re.DOTALL)


def _failure_text(case: ET.Element) -> str | None:
    """First line of a junit failure/error message, as a raw fact for the agent."""
    node = case.find("failure")
    if node is None:
        node = case.find("error")
    if node is None:
        return None
    msg = (node.get("message") or node.text or "").strip()
    return msg.splitlines()[0][:400] if msg else ""


def _capability_findings(capability_dir: Path, single_report: Path) -> dict[str, Any]:
    """Aggregate the capability junit reports across K repeats into per-probe counts.

    Reads every ``*.xml`` under ``capability_dir`` (one per repeat), falling back to
    a single ``single_report`` file for a K=1 run. Skipped cases are not counted as
    runs. Each probe gets ``passed/runs`` plus the raw failure messages seen - no
    band, no verdict; the agent decides what the pass-rate means.
    """
    files = sorted(glob.glob(str(capability_dir / "*.xml")))
    if not files and single_report.exists():
        files = [str(single_report)]
    if not files:
        return {
            "report_present": False,
            "all_passed": None,
            "probes": 0,
            "runs_per_probe": 0,
            "probes_with_failures": 0,
            "unparseable_reports": 0,
            "per_probe": [],
        }

    passed: Counter = Counter()
    runs: Counter = Counter()
    failures: dict[str, Counter] = {}
    unparseable = 0
    for report in files:
        # A junit written by a worker that was OOM-killed mid-flush can be
        # truncated; skip it (and count it) rather than aborting the whole
        # aggregation, mirroring the trajectory loop below.
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError):
            unparseable += 1
            continue
        for case in root.iter("testcase"):
            name = case.get("name", "")
            if case.find("skipped") is not None:
                continue
            runs[name] += 1
            if case.find("failure") is None and case.find("error") is None:
                passed[name] += 1
            else:
                text = _failure_text(case)
                if text is not None:
                    failures.setdefault(name, Counter())[text] += 1

    per_probe: list[dict[str, Any]] = []
    for name in sorted(runs):
        n_runs = runs[name]
        n_pass = passed[name]
        per_probe.append(
            {
                "probe": name,
                "passed": n_pass,
                "runs": n_runs,
                "pass_rate": round(n_pass / n_runs, 3) if n_runs else 0.0,
                # Raw distinct failure messages (with how often each was seen) so
                # the agent can read the actual assertion text, not our reading of it.
                "failure_messages": [
                    {"message": msg, "count": cnt}
                    for msg, cnt in failures.get(name, Counter()).most_common()
                ],
            }
        )

    probes_with_failures = sum(1 for p in per_probe if p["passed"] < p["runs"])
    return {
        "report_present": True,
        "all_passed": probes_with_failures == 0,
        "probes": len(per_probe),
        "runs_per_probe": max((p["runs"] for p in per_probe), default=0),
        "probes_with_failures": probes_with_failures,
        "unparseable_reports": unparseable,
        "per_probe": per_probe,
    }


def _excerpt(obj: Any, limit: int = 300) -> str:
    try:
        text = yaml.safe_dump(obj, default_flow_style=True, sort_keys=False).strip()
    except (yaml.YAMLError, TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _wire_findings(obs_dir: Path) -> dict[str, Any]:
    # Union every wire_probes_* run under the observation dir: the observe stage
    # writes one, the reprobe stage writes one per failed task. Fall back to a bare
    # trials/ dir if the runs were not timestamp-nested.
    traj_paths = sorted(
        glob.glob(str(obs_dir / "wire_probes_*" / "trials" / "*" / "*" / "trajectory.yaml"))
    )
    if not traj_paths:
        traj_paths = sorted(glob.glob(str(obs_dir / "trials" / "*" / "*" / "trajectory.yaml")))
    tasks: set[str] = set()
    task_trials: Counter = Counter()
    task_rej_trials: Counter = Counter()
    tool_call_count = 0
    rej_by_task: Counter = Counter()
    rej_by_tool: Counter = Counter()
    messages: set[str] = set()
    # Raw (task, tool) -> a rejected arguments excerpt. Facts only; whether a shape
    # is a preset-fixable formatting quirk or a genuine model error is the agent's call.
    rejected_examples: dict[tuple[str, str], dict[str, Any]] = {}
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
            if key not in rejected_examples:
                rejected_examples[key] = {
                    "task": task,
                    "tool": tool,
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
        "any_rejection": rejecting_trials > 0,
        "tool_arg_rejections": {
            "total": sum(rej_by_task.values()),
            "rejecting_trials": rejecting_trials,
            "trial_rate": round(rejecting_trials / trials, 3) if trials else 0.0,
            "by_task": dict(rej_by_task.most_common()),
            "by_task_trial_rate": by_task_trial_rate,
            "by_tool": dict(rej_by_tool.most_common()),
            "messages": sorted(messages),
        },
        "rejected_examples": list(rejected_examples.values()),
        "infra": {
            "rate_limit": term.get("rate_limit", 0),
            "status_error": status_error,
            "max_turns": term.get("max_turns", 0),
            "stuck": term.get("stuck_detected", 0),
            # Without these, a wire run that died on provider 5xx/timeouts on every
            # trial reads as "clean" (no rejections, all-zero infra).
            "api_error": term.get("api_error", 0),
            "api_timeout": term.get("api_timeout", 0),
        },
    }


def build_findings(obs_dir: Path) -> dict[str, Any]:
    """Assemble the raw-stat findings structure from an observe artifact directory."""
    manifest_path = obs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    capability = _capability_findings(obs_dir / "capability", obs_dir / "capability_report.xml")
    # Observe-only structural-variant suite (the legacy-test `test_variant_*` files),
    # aggregated the same way. Absent on runs without it.
    variants = _capability_findings(obs_dir / "variants", obs_dir / "__no_single_variant_report__")
    wire = _wire_findings(obs_dir)

    # A present-but-empty capability report (every probe SKIPPED - e.g. a missing or
    # empty API key makes live_client skip everything) must NOT read as "all passed":
    # zero probes ran, so nothing was verified. Require the suite to have executed at
    # least one probe before the aggregate can be green. This does not false-negative
    # on legitimate known_unsupported skips, because the all-required candidate cert
    # always yields probes > 0 when the key works.
    capability_ran = bool(capability["report_present"]) and capability["probes"] > 0
    all_passed = (
        capability_ran
        and capability["all_passed"]
        and (variants["all_passed"] is None or variants["all_passed"])
        and not wire["any_rejection"]
    )
    notes = [
        "RAW STATS ONLY: this file bands nothing and judges nothing. `all_passed`"
        " is the only verdict; per-probe pass counts, failure_messages and"
        " rejected_examples are raw facts for the agent to analyze.",
        "wire probes are non-scoring: grade.binary_pass, aggregate.success_rate,"
        " failure_attribution.json and metrics.tool_success_rate do NOT reflect"
        " tool-argument rejections; wire.tool_arg_rejections is the faithful signal.",
    ]
    # For the observe stage, 0 capability probes is an infra failure worth flagging; on a
    # reprobe artifact a missing capability section can be the deliberate wire-only pass,
    # so the note would mislabel it.
    if not capability_ran and manifest.get("stage", "observe") == "observe":
        notes.insert(
            0,
            "capability suite did NOT execute (0 probes ran - report absent or every"
            " probe skipped, e.g. a missing/empty API key). all_passed is forced False;"
            " this is an infra failure, not a clean pass.",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        # The reprobe stage writes its own manifest (stage=reprobe) before calling this;
        # carrying it through keeps the artifact self-describing and lets the summary
        # renderer tell a deliberate wire-only pass from an observe whose capability
        # suite failed to run.
        "stage": manifest.get("stage", "observe"),
        "candidate": manifest.get("candidate", {}),
        "preset": manifest.get("preset", "default"),
        # The single yes/no the deterministic layer commits to. Everything else is
        # raw stats + raw error text for the agent to analyze.
        "all_passed": all_passed,
        "capability_ran": capability_ran,
        "capability": capability,
        "variants": variants,
        "wire": wire,
        "notes": notes,
    }


# The wire termination reasons that make an observe run untrustworthy. max_turns and stuck are
# deliberately NOT here: both can be genuine model behaviour (see the four-bucket taxonomy in
# prompts/_shared_context.md), so they are data for the resolve agent, not contamination.
GATE_INFRA_KEYS = ("rate_limit", "api_error", "api_timeout", "status_error")


def evaluate_gate(findings: dict[str, Any]) -> tuple[bool, str]:
    """Observe cleanliness gate: may a clean observe chain into resolve?

    Lives here rather than inline in the workflow because an inline check cannot be
    unit-tested and silently drifts from :data:`GATE_INFRA_KEYS`. Both suites must have
    RUN (the wire step is ``|| true``-guarded in the workflow, so a startup crash shows
    up only as 0 trials). Returns ``(clean, reason)``; ``reason`` is empty when clean.
    """
    if not findings.get("capability_ran"):
        return False, "capability suite did not run (0 probes)"
    wire = findings.get("wire") or {}
    if not wire.get("trials"):
        return False, "wire probes did not run (0 trials)"
    infra = wire.get("infra") or {}
    contaminated = {key: infra.get(key, 0) for key in GATE_INFRA_KEYS if infra.get(key, 0)}
    if contaminated:
        detail = ", ".join(f"{key}={count}" for key, count in sorted(contaminated.items()))
        return False, f"wire infra contamination ({detail})"
    return True, ""


def gate(findings_path: str) -> int:
    """Print the observe gate token and return 0 (the workflow branches on stdout).

    ``clean`` when resolve may run; ``dirty: <reason>`` otherwise, the reason surfacing
    verbatim in the needs-human notification. A missing/unreadable findings file is dirty
    (the observe crashed before aggregation), never an exception - the gate must always
    hand the workflow a token it can route on.
    """
    try:
        findings = json.loads(Path(findings_path).read_text())
    except (OSError, ValueError) as exc:
        print(f"dirty: findings unreadable - observe crashed before aggregation ({exc})")
        return 0
    clean, reason = evaluate_gate(findings)
    print("clean" if clean else f"dirty: {reason}")
    return 0


def render_summary(findings: dict[str, Any], run_url: str | None = None) -> str:
    """Render a short human-readable markdown summary of the raw stats."""
    cap = findings.get("capability", {})
    var = findings.get("variants", {})
    wire = findings.get("wire", {})
    rej = wire.get("tool_arg_rejections", {})
    candidate = (findings.get("candidate") or {}).get("name", "?")
    preset = findings.get("preset", "default")
    stage = findings.get("stage", "observe")

    # A reprobe artifact with no capability section is the deliberate wire-only pass, not a
    # capability suite that failed to run - its verdict is the wire result.
    wire_only = stage == "reprobe" and not cap.get("report_present")
    if wire_only:
        if wire.get("trials"):
            verdict = (
                f"wire-only pass: {rej.get('rejecting_trials', 0)}/{wire.get('trials', 0)} "
                "trials with a tool-arg rejection"
            )
        else:
            verdict = "wire-only pass produced no trials (the wire run failed to start)"
    elif not findings.get("capability_ran", True):
        verdict = "capability suite did NOT run (0 probes) - infra failure, not a pass"
    elif findings.get("all_passed"):
        verdict = "all probes passed"
    else:
        verdict = "failures present"
    lines = [
        f"### Auto-integration {stage}: `{candidate}` on the `{preset}` preset",
        "",
        f"**{verdict}** (raw stats only; the agent analyzes the errors).",
        "",
    ]

    def _section(title: str, sec: dict[str, Any]) -> None:
        if not sec.get("report_present"):
            return
        lines.append(
            f"- {title} ({sec.get('probes', 0)} probes x {sec.get('runs_per_probe', 0)} reps): "
            f"{sec.get('probes_with_failures', 0)} probe(s) with at least one failure."
        )
        for probe in sec.get("per_probe", []):
            if probe["passed"] < probe["runs"]:
                lines.append(f"  - `{probe['probe']}`: {probe['passed']}/{probe['runs']} passed")

    _section("Capability", cap)
    _section("Shape variants", var)

    lines.append(
        f"- Wire ({wire.get('trials', 0)} trials, {wire.get('tool_call_count', 0)} tool calls): "
        f"**{rej.get('rejecting_trials', 0)}/{wire.get('trials', 0)} trials with a tool-arg rejection** "
        f"(rate {rej.get('trial_rate', 0)})."
    )
    for shape in wire.get("rejected_examples", []):
        task = shape["task"]
        rate = rej.get("by_task_trial_rate", {}).get(task)
        rate_str = f", {rate} of trials" if rate is not None else ""
        lines.append(f"  - `{task}` / `{shape['tool']}` rejected{rate_str}")
    infra = wire.get("infra", {})
    # Every gate key plus the model-attributable pair: the PR-visible summary must show
    # the same counters the gate dirties on, or an api_timeout needs-human reads as
    # "all-zero infra" here and the reason only ever appears in Slack.
    infra_keys = (*GATE_INFRA_KEYS, "max_turns", "stuck")
    lines.append("- Infra: " + ", ".join(f"{key}={infra.get(key, 0)}" for key in infra_keys) + ".")
    lines.append("")
    tail = "Full artifact (capability reports + trajectories + `findings.json`) for the next step."
    if run_url:
        tail += f" [Run]({run_url})"
    lines.append(tail)
    return "\n".join(lines) + "\n"


def run(
    obs_dir: str,
    out: str | None = None,
    summary_out: str | None = None,
    run_url: str | None = None,
) -> int:
    """Build the findings from ``obs_dir``, write ``findings.json`` (and an optional
    markdown summary), and return 0."""
    obs = Path(obs_dir)
    findings = build_findings(obs)
    out_path = Path(out) if out else obs / "findings.json"
    out_path.write_text(json.dumps(findings, indent=2) + "\n")
    if summary_out:
        Path(summary_out).write_text(render_summary(findings, run_url))

    cap = findings["capability"]
    rej = findings["wire"]["tool_arg_rejections"]
    print(
        f"findings: all_passed={findings['all_passed']}; capability "
        f"{cap.get('probes', 0)} probes x {cap.get('runs_per_probe', 0)} reps "
        f"({cap.get('probes_with_failures', 0)} with failures); wire "
        f"{rej.get('rejecting_trials', 0)}/{findings['wire'].get('trials', 0)} trials rejected; "
        f"wrote {out_path}"
    )
    return 0
