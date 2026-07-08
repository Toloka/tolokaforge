#!/usr/bin/env python
"""Targeted re-probe for the model auto-integration RESOLVE phase.

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

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Import the deterministic collector that lives next to this script. Running
# ``python scripts/integration/reprobe.py`` puts this dir on sys.path[0]; the
# insert makes ``import observe_findings`` work under other invocations too.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import observe_findings  # noqa: E402

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
    subprocess.run([*RUN_CMD, "run", "--config", str(cfg_path), "--verbose"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-run only the failed probes under a policy overlay and emit findings."
    )
    parser.add_argument(
        "--baseline", required=True, help="observe findings.json to read failures from"
    )
    parser.add_argument("--overlay", required=True, help="policy preset overlay YAML (the fix)")
    parser.add_argument("--provider", required=True, help="candidate provider (e.g. openrouter)")
    parser.add_argument(
        "--name", required=True, help="candidate model slug (e.g. minimax/minimax-m3)"
    )
    parser.add_argument("--out", required=True, help="output dir for the re-probe observation")
    parser.add_argument("--dataset", default=WIRE_DATASET, help="wire task-pack root")
    parser.add_argument("--capability-k", type=int, default=15)
    parser.add_argument("--wire-k", type=int, default=10)
    parser.add_argument("--workers", type=int, default=10)
    # Flat (probe x rep) pool width. Bounded by the OpenRouter rate limit (~10-16 safe;
    # higher risks 429s that erase the gain, since the key does not rotate).
    parser.add_argument("--cap-parallel", type=int, default=10)
    parser.add_argument(
        "--targets",
        default=None,
        help="comma-separated probe names to reprobe (the agent's fix_targets); default = "
        "ALL failed probes from the baseline. Restricting to fix_targets skips the slow, "
        "un-fixable ceiling probes (thinking/caching) each iteration.",
    )
    parser.add_argument(
        "--skip-wire", action="store_true", help="capability-only (the agent's inner loop)"
    )
    parser.add_argument("--run-url", default=None)
    args = parser.parse_args()

    from tolokaforge.core.output.artifacts import model_id_slug

    model_id = model_id_slug(args.provider, args.name)
    baseline = json.loads(Path(args.baseline).read_text())
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Which probes to reprobe: the agent's fix_targets if given (skips the slow, un-fixable
    # ceiling probes), else ALL failed probes from the baseline (capability + variants).
    if args.targets:
        probes = [t.strip() for t in args.targets.split(",") if t.strip()]
    else:
        probes = failed_probes(baseline.get("capability")) + failed_probes(baseline.get("variants"))
    n_var = sum(1 for p in probes if p.startswith("test_variant"))
    wire_tasks = [] if args.skip_wire else failed_wire_tasks(baseline)
    print(
        f"re-probe targets: {len(probes) - n_var} capability, {n_var} variant, "
        f"{len(wire_tasks)} wire task(s) under overlay {args.overlay} "
        f"(flat pool, K={args.capability_k}, width={args.cap_parallel})"
    )

    run_capability_flat(
        probes=probes,
        overlay=args.overlay,
        provider=args.provider,
        name=args.name,
        model_id=model_id,
        out_dir=out_dir,
        k=args.capability_k,
        workers=args.cap_parallel,
    )
    for task_id in wire_tasks:
        run_wire_task(
            task_id=task_id,
            overlay=args.overlay,
            provider=args.provider,
            name=args.name,
            out_dir=out_dir,
            dataset=args.dataset,
            k=args.wire_k,
            workers=args.workers,
        )

    # Tag the findings with the policy under test so the agent can diff against baseline.
    manifest = {
        "schema_version": 1,
        "stage": "reprobe",
        "candidate": {"provider": args.provider, "name": args.name, "model_id": model_id},
        "preset": Path(args.overlay).stem,
        "baseline": str(args.baseline),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    findings = observe_findings.build_findings(out_dir)
    (out_dir / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    (out_dir / "summary.md").write_text(observe_findings.render_summary(findings, args.run_url))
    print(
        f"re-probe findings: all_passed={findings['all_passed']} "
        f"(capability_ran={findings['capability_ran']}); wrote {out_dir / 'findings.json'}"
    )


if __name__ == "__main__":
    main()
