"""Flat (node x rep) parallel probe runner for the OBSERVE stage.

The old observe steps parallelized at the REP level (``seq 1 K | xargs -P W`` where each rep
was ONE pytest that ran every matching node SERIALLY). For a slow reasoning model that spends
minutes-per-call (and reasons heavily on the shape-sensitive variant cases), a rep is a long
serial chain and the W-wide rep pool barely helps: deepseek-v3.2-exp spent ~5h on the variant
step alone at K=15 / W=4.

This flattens it: collect the candidate's selected pytest nodes ONCE (``--collect-only``), then
run each ``(node x rep)`` as its own single-node pytest into ``<out>/u<idx>_rep<rep>.xml``,
parallelized at ``--workers``. So ``n_nodes x K`` units run concurrently up to the width, not
``W`` long serial reps. Same trick as ``reprobe`` (resolve stage); the observe stage never
got it. The collector (``observe``) aggregates by testcase NAME across all ``*.xml``, so the
junit filenames are free-form.

Report-only: a failing probe is data for the next stage, never a gate, so a non-zero pytest
exit is swallowed. Env (TF_CANDIDATE_* / MODEL_ID / OPENROUTER_API_KEY) is inherited from the
caller, exactly as the old inline step relied on.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PYTEST = ["uv", "run", "pytest"]
DEFAULT_PYARGS = "tolokaforge.testing.certify.suite"


def collect_nodes(k_expr: str, pyargs: str = DEFAULT_PYARGS) -> list[str]:
    """Collect the pytest node ids matching ``-k k_expr`` (one --collect-only pass).

    Collects via ``--pyargs`` against the installed suite package so the
    caller works from a fresh ``uv sync`` clone or a worktree checkout
    without encoding a "tests live inside this checkout" assumption.
    """
    proc = subprocess.run(
        # `-o addopts=` clears the repo's addopts (which force a verbose TREE collect layout);
        # with them cleared, `-q --collect-only` prints one flat `path::node[param]` per line.
        [
            *PYTEST,
            "--pyargs",
            pyargs,
            "-k",
            k_expr,
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Flat node ids contain "::"; the trailing summary / header lines do not and are dropped.
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


def build_units(nodes: list[str], reps: int) -> list[tuple[int, str, int]]:
    """Flat pool of ``(unit_index, node_id, rep)`` across every node x rep."""
    return [(idx, node, rep) for idx, node in enumerate(nodes) for rep in range(1, reps + 1)]


def _run_unit(out: Path, idx: int, node: str, rep: int) -> None:
    junit = out / f"u{idx}_rep{rep}.xml"
    subprocess.run(
        [*PYTEST, node, f"--junitxml={junit}", "-q", "--tb=line", "-p", "no:cacheprovider"],
        check=False,
    )


def run(
    k_expr: str,
    out: str,
    reps: int = 15,
    workers: int = 10,
    pyargs: str = DEFAULT_PYARGS,
) -> int:
    """Collect the candidate's nodes and run each ``(node x rep)`` unit into ``out``,
    parallelized at ``workers``. Report-only: always returns 0 (a failing probe is data
    for the next stage, not a gate)."""
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = collect_nodes(k_expr, pyargs)
    if not nodes:
        print(f"run_probes: no nodes matched -k {k_expr!r} under --pyargs {pyargs}")
        return 0
    units = build_units(nodes, reps)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(lambda u: _run_unit(out_dir, *u), units))
    print(
        f"run_probes: {len(nodes)} nodes x {reps} reps = {len(units)} units "
        f"at width {workers} -> {out_dir}"
    )
    return 0
