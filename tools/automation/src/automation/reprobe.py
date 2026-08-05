"""Targeted re-probe for the model automation RESOLVE phase.

Given the observe baseline findings and a policy overlay (the preset the resolve
agent SET or CREATED), re-run ONLY the probes that failed in the baseline, under
that overlay, and emit fresh findings. Green means the policy fixed it; still red
means it is not a formatting issue but a capability/reasoning ceiling.

The overlay is the engine's native preset overlay (``--presets-file`` /
``RunConfig.engine.presets_file``); it can only compose EXISTING adapter classes
(schema_sanitizer / prompt_policy / response_policy / ...), so "create a policy"
means a new combination of shipped building blocks, not new engine code.

Topology-neutral: the resolve agent calls this as a tool inside its fix loop, and
CI can invoke it as a step. It re-runs the cheap capability probes (and any failed
shape variants) plus the failed wire tasks; the agent typically iterates on
capability-only and does the wire pass once at the end. The Docker core image and
the OPENROUTER_API_KEY are the caller's responsibility (same as the observe stage).

Selection is precise: a failed probe ``func[param]`` re-runs as the ``-k`` group
``(func and param)`` (no brackets, which pytest ``-k`` rejects), so a shape id that
appears in more than one test (e.g. ``nested_in_object``) never cross-selects.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from automation import observe

# The user-sim and wire task pack, matching the observe stage.
WIRE_DATASET = "tests/data/tasks/wire_probes/dataset"
USER_SIM = {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6", "temperature": 0.2}
RUN_CMD = ["uv", "run", "tolokaforge"]
PYTEST_CMD = ["uv", "run", "pytest"]


def failed_probes(section: dict[str, Any]) -> list[str]:
    """Probe names in a findings section (capability/variants) that did not fully pass."""
    return [
        p["probe"]
        for p in (section or {}).get("per_probe", [])
        if p.get("passed", 0) < p.get("runs", 0)
    ]


def failed_wire_tasks(findings: dict[str, Any]) -> list[str]:
    """Wire task ids that had at least one tool-argument rejection in the baseline."""
    by_task = (((findings or {}).get("wire") or {}).get("tool_arg_rejections") or {}).get("by_task")
    return sorted((by_task or {}).keys())


def validate_selection_flags(targets: str | None, wire_only: bool, skip_wire: bool) -> None:
    """Reject conflicting selection flags; raises ``ValueError`` naming the conflict.

    One rule, two callers: the CLI pre-checks so a flag conflict is a usage error (exit 2,
    no traceback), and :func:`select_reprobe_targets` enforces it for library callers.
    """
    if wire_only and skip_wire:
        raise ValueError("--wire-only and --skip-wire are mutually exclusive")
    if wire_only and targets:
        raise ValueError(
            "--wire-only re-runs the baseline's rejecting wire tasks; --targets selects "
            "capability probes - pass one or the other"
        )


def select_reprobe_targets(
    baseline: dict[str, Any],
    targets: str | None,
    wire_only: bool,
    skip_wire: bool,
) -> tuple[list[str], list[str]]:
    """The ``(probes, wire_tasks)`` a reprobe run should re-run. Pure, fail-fast.

    ``wire_only`` is the final wire-verification pass: NO capability probes, only the
    baseline's rejecting wire tasks (the fix loop iterates with ``skip_wire``, so without
    this pass a fix whose only live evidence was the wire would ship unmeasured).
    Conflicting flags raise instead of guessing.
    """
    validate_selection_flags(targets, wire_only, skip_wire)
    if wire_only:
        return [], failed_wire_tasks(baseline)
    if targets:
        probes = [t.strip() for t in targets.split(",") if t.strip()]
    else:
        probes = failed_probes(baseline.get("capability")) + failed_probes(baseline.get("variants"))
    return probes, ([] if skip_wire else failed_wire_tasks(baseline))


def k_group(probe_name: str) -> str:
    """Turn a junit probe name ``func[param]`` into a precise ``-k`` group.

    ``(func and param)`` matches exactly one node without using ``[``/``]`` (which
    pytest ``-k`` cannot parse), and the ``and`` prevents a shared param id from
    cross-selecting a same-named case in another test.
    """
    if "[" in probe_name and probe_name.endswith("]"):
        func, param = probe_name.split("[", 1)
        return f"({func} and {param[:-1]})"
    return f"({probe_name})"


def _parallel(fns: list[Any], workers: int) -> None:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(lambda f: f(), fns))


def run_capability_flat(
    *,
    probes: list[str],
    overlay: str,
    provider: str,
    name: str,
    model_id: str,
    out_dir: Path,
    k: int,
    workers: int,
) -> None:
    """Re-run each probe K times under the overlay, as a FLAT (probe x rep) pool.

    Each unit is a single-probe pytest (``-k (func and param)``), so the pool parallelizes
    across BOTH probes and repeats at ``workers`` width, instead of the old per-rep pool that
    ran every probe serially inside each rep. For a latency-bound model (slow reasoning route)
    this hides the per-call latency: n_probes x K concurrent calls up to the width cap, rather
    than n_probes serial per rep. Variant probes (``test_variant_*``) get TF_RUN_VARIANTS=1 and
    their junit lands in ``variants/``; every other probe in ``capability/`` (so the collector
    aggregates each side correctly). Width is bounded by the OpenRouter rate limit, not raised
    without care: too high and 429s erase the gain.
    """
    if not probes:
        return
    (out_dir / "capability").mkdir(parents=True, exist_ok=True)
    (out_dir / "variants").mkdir(parents=True, exist_ok=True)
    base_env = {
        "TF_PRESETS_FILE": overlay,
        "TF_CANDIDATE_PROVIDER": provider,
        "TF_CANDIDATE_NAME": name,
        "MODEL_ID": model_id,
    }

    def _unit(probe: str, idx: int, rep: int) -> Any:
        import os

        is_variant = probe.startswith("test_variant")
        subdir = "variants" if is_variant else "capability"
        junit = out_dir / subdir / f"u{idx}_rep{rep}.xml"
        env = {**os.environ, **base_env}
        if is_variant:
            env["TF_RUN_VARIANTS"] = "1"
        cmd = [
            *PYTEST_CMD,
            "tests/integration/llm/",
            "-k",
            k_group(probe),
            f"--junitxml={junit}",
            "-q",
            "--tb=line",
            "-p",
            "no:cacheprovider",
        ]
        return lambda: subprocess.run(cmd, env=env, check=False)

    units = [_unit(probe, idx, rep) for idx, probe in enumerate(probes) for rep in range(1, k + 1)]
    _parallel(units, workers)


def run_wire_task(
    *,
    task_id: str,
    overlay: str,
    provider: str,
    name: str,
    out_dir: Path,
    dataset: str,
    k: int,
    workers: int,
) -> None:
    """Re-run a single failed wire task under the overlay into its own wire_probes dir.

    Pack root stays the dataset (so ``_shared`` tools resolve); ``tasks_glob`` narrows
    to just this task. Each task writes a separate ``wire_probes_<ts>`` dir; the
    collector unions them.
    """
    cfg = {
        "models": {
            "agent": {"provider": provider, "name": name, "temperature": 0.0, "max_tokens": 4096},
            "user": dict(USER_SIM),
        },
        "orchestrator": {
            "workers": workers,
            "repeats": k,
            "max_turns": 10,
            "queue_backend": "sqlite",
        },
        "evaluation": {
            "task_packs": [dataset],
            "tasks_glob": f"**/{task_id}/task.yaml",
            "output_dir": str(out_dir / "wire_probes"),
        },
        "engine": {"presets_file": overlay},
    }
    cfg_path = out_dir / f"reprobe_{task_id}.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    # No --verbose: INFO-level keeps the reprobe log readable (results come from findings.json).
    subprocess.run([*RUN_CMD, "run", "--config", str(cfg_path)], check=False)


def run(
    *,
    baseline: str,
    overlay: str,
    provider: str,
    name: str,
    out: str,
    dataset: str = WIRE_DATASET,
    capability_k: int = 15,
    wire_k: int = 10,
    workers: int = 10,
    cap_parallel: int = 10,
    targets: str | None = None,
    skip_wire: bool = False,
    wire_only: bool = False,
    run_url: str | None = None,
) -> int:
    """Re-run only the failed probes under a policy overlay and emit findings. Returns 0."""
    from tolokaforge.core.output.artifacts import model_id_slug

    model_id = model_id_slug(provider, name)
    baseline_findings = json.loads(Path(baseline).read_text())
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Which probes to reprobe: the agent's fix_targets if given (skips the slow, un-fixable
    # ceiling probes), else ALL failed probes from the baseline (capability + variants) -
    # or, for the final wire-verification pass, no probes and only the rejecting wire tasks.
    probes, wire_tasks = select_reprobe_targets(baseline_findings, targets, wire_only, skip_wire)
    n_var = sum(1 for p in probes if p.startswith("test_variant"))
    print(
        f"re-probe targets: {len(probes) - n_var} capability, {n_var} variant, "
        f"{len(wire_tasks)} wire task(s) under overlay {overlay} "
        f"(flat pool, K={capability_k}, width={cap_parallel})"
    )

    run_capability_flat(
        probes=probes,
        overlay=overlay,
        provider=provider,
        name=name,
        model_id=model_id,
        out_dir=out_dir,
        k=capability_k,
        workers=cap_parallel,
    )
    for task_id in wire_tasks:
        run_wire_task(
            task_id=task_id,
            overlay=overlay,
            provider=provider,
            name=name,
            out_dir=out_dir,
            dataset=dataset,
            k=wire_k,
            workers=workers,
        )

    # Tag the findings with the policy under test so the agent can diff against baseline.
    manifest = {
        "schema_version": 1,
        "stage": "reprobe",
        "candidate": {"provider": provider, "name": name, "model_id": model_id},
        "preset": Path(overlay).stem,
        "baseline": str(baseline),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    findings = observe.build_findings(out_dir)
    (out_dir / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    (out_dir / "summary.md").write_text(observe.render_summary(findings, run_url))
    print(
        f"re-probe findings: all_passed={findings['all_passed']} "
        f"(capability_ran={findings['capability_ran']}); wrote {out_dir / 'findings.json'}"
    )
    return 0
