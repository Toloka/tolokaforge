"""Measure what writing a trial's final environment state costs the engine host.

Builds a synthetic final state — many tables of records with mixed scalars, the
shape a database-backed world reports — and times each way of putting it in a
trial bundle, each in a fresh subprocess so one variant's heap does not inflate
the next one's peak. Reports wall time, the peak resident set size of the
process, and the growth of that peak over the process's size once the state was
built (``peak - at rest``), which is what the write itself cost.

Variants:

``yaml_dump``
    What ``OutputWriter.write_env_state`` did before the streaming writer:
    ``yaml.dump`` with the pure-Python dumper.
``streaming_python``
    :func:`tolokaforge.core.streaming_yaml.dump_mapping_streaming` with
    PyYAML's Python emitter (byte-identical output).
``streaming_libyaml``
    The same walk with libyaml's emitter — what ``write_env_state`` uses now
    when PyYAML was built with libyaml.
``by_reference``
    The host's side of ``final_state_delivery: mount`` (ADR-0069): the state
    was written to a file by another process (the runner), and this process
    checks the file's SHA-256 and size, writes the small ``env.yaml`` that
    references it and copies it into the bundle. It never builds the state, so
    its "at rest" is a bare interpreter with the engine imported.
``runner_state_file``
    The runner's side of the same delivery: ``write_state_file`` on a built
    state, against ``json.dumps`` of it (the inline ``state_json``), reported
    as the extra peak each needs over the built state.

Usage::

    uv run --offline python scripts/analysis/bench_final_state_write.py --mb 100
    uv run --offline python scripts/analysis/bench_final_state_write.py --mb 100 --variant yaml_dump

Not part of the test suite: at a few hundred MB the ``yaml_dump`` variant takes
minutes and gigabytes, which is the point of measuring it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

VARIANTS = (
    "yaml_dump",
    "streaming_python",
    "streaming_libyaml",
    "by_reference",
    "runner_state_file",
)

_RECORD_TEMPLATE_BYTES = 360
"""Approximate JSON size of one synthetic record, used to size the state."""


def _rss_bytes() -> int:
    """Peak RSS of this process so far (``ru_maxrss`` is KiB on Linux, bytes on macOS)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _current_rss_bytes() -> int:
    """Resident set size now, where the platform reports it; else the peak so far."""
    try:
        with open(f"/proc/{os.getpid()}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
        )
        return int(out.stdout.strip()) * 1024 if out.stdout.strip() else _rss_bytes()


def build_state(target_mb: float, seed: int = 7) -> dict[str, list[dict[str, Any]]]:
    """A synthetic ``{table: [record, ...]}`` state whose JSON is about ``target_mb`` MiB."""
    rng = random.Random(seed)
    records = max(1, int(target_mb * 1024 * 1024 / _RECORD_TEMPLATE_BYTES))
    tables = 40
    state: dict[str, list[dict[str, Any]]] = {}
    for t in range(tables):
        rows = []
        for i in range(records // tables):
            rows.append(
                {
                    "id": f"T{t:02d}-{i:08d}",
                    "seq": i,
                    "amount": round(rng.uniform(-1e6, 1e6), 2),
                    "ratio": rng.random(),
                    "active": rng.random() < 0.5,
                    "deleted_at": None,
                    "status": rng.choice(("open", "closed", "pending", "yes", "null")),
                    "name": f"customer {rng.randrange(10**6)} of segment {t}",
                    "note": "first line\nsecond line" if i % 17 == 0 else "plain text note",
                    "tags": [rng.randrange(1000) for _ in range(3)],
                    "address": {"city": "Springfield", "zip": f"{rng.randrange(10**5):05d}"},
                    "created": "2026-01-01T00:00:00Z",
                }
            )
        state[f"table_{t:02d}"] = rows
    return state


def env_state_of(state: dict[str, Any]) -> dict[str, Any]:
    """The ``env.yaml`` mapping the conductor builds from a captured state, inline delivery.

    ``EnvironmentState.get_final_state`` keeps two deep copies of the state —
    ``agent`` and the legacy ``db`` — so the inline ``env.yaml`` carries it twice.
    """
    return {
        "agent": state,
        "user": {"device": {}},
        "db": copy.deepcopy(state),
        "filesystem": {},
        "agent_visible_dir": "/work",
    }


def _run_by_reference(target_mb: float, out_dir: Path) -> dict[str, Any]:
    """Host side of the by-reference delivery, after another process wrote the file."""
    from tolokaforge.core.output_writer import FINAL_STATE_REF_KEY, OutputWriter
    from tolokaforge.runner.final_state_file import (
        StateFile,
        resolve_state_file,
        verify_state_file,
    )

    runner_dir = out_dir / "runner"
    runner_dir.mkdir()
    prepare = subprocess.run(
        [
            sys.executable,
            "-c",
            _PREPARE.format(
                here=str(Path(__file__).resolve().parent), mb=target_mb, out=str(runner_dir)
            ),
        ],
        capture_output=True,
        text=True,
    )
    if prepare.returncode != 0:
        raise SystemExit(prepare.stderr)
    reported = json.loads(prepare.stdout)
    at_rest = _current_rss_bytes()
    started = time.perf_counter()
    state_file = resolve_state_file(runner_dir, StateFile(**reported))
    verify_state_file(state_file)
    env_state = {
        FINAL_STATE_REF_KEY: state_file.as_ref(),
        "filesystem": {},
        "agent_visible_dir": "/work",
    }
    OutputWriter(out_dir / "bundle").write_env_state(env_state, state_file)
    elapsed = time.perf_counter() - started
    peak = _rss_bytes()
    return {
        "variant": "by_reference",
        "state_json_mib": round(state_file.size / 2**20, 1),
        "output_mib": round((out_dir / "bundle" / "env.yaml").stat().st_size / 2**20, 4),
        "wall_s": round(elapsed, 2),
        "rss_at_rest_mib": round(at_rest / 2**20),
        "rss_peak_mib": round(peak / 2**20),
        "write_cost_mib": round(max(0, peak - at_rest) / 2**20),
    }


_PREPARE = (
    "import json, pathlib, sys\n"
    "sys.path.insert(0, {here!r})\n"
    "from bench_final_state_write import build_state\n"
    "from tolokaforge.runner.final_state_file import write_state_file\n"
    "written = write_state_file(pathlib.Path({out!r}), build_state({mb}))\n"
    "print(json.dumps({{'path': written.path, 'sha256': written.sha256, 'size': written.size}}))\n"
)


def _run_runner_state_file(target_mb: float, out_dir: Path) -> dict[str, Any]:
    """Extra peak the runner needs to answer inline (one string) versus by reference."""
    from tolokaforge.runner.final_state_file import write_state_file

    state = build_state(target_mb)
    at_rest = _current_rss_bytes()
    started = time.perf_counter()
    write_state_file(out_dir, state)
    elapsed = time.perf_counter() - started
    peak_file = _rss_bytes()
    started_inline = time.perf_counter()
    text = json.dumps(state)
    inline_elapsed = time.perf_counter() - started_inline
    peak_inline = _rss_bytes()
    return {
        "variant": "runner_state_file",
        "state_json_mib": round(len(text) / 2**20, 1),
        "wall_s": round(elapsed, 2),
        "json_dumps_wall_s": round(inline_elapsed, 2),
        "rss_at_rest_mib": round(at_rest / 2**20),
        "write_cost_mib": round(max(0, peak_file - at_rest) / 2**20),
        "json_dumps_cost_mib": round(max(0, peak_inline - at_rest) / 2**20),
    }


def _run_variant(variant: str, target_mb: float, out_dir: Path) -> dict[str, Any]:
    if variant == "by_reference":
        return _run_by_reference(target_mb, out_dir)
    if variant == "runner_state_file":
        return _run_runner_state_file(target_mb, out_dir)

    import yaml

    import tolokaforge.core.output_writer  # noqa: F401 - registers the engine's representers
    from tolokaforge.core.streaming_yaml import dump_mapping_streaming

    env_state = env_state_of(build_state(target_mb))
    state_json_bytes = len(json.dumps(env_state["db"]).encode())
    at_rest = _current_rss_bytes()
    out = out_dir / f"{variant}.yaml"
    started = time.perf_counter()
    with open(out, "w") as f:
        if variant == "yaml_dump":
            yaml.dump(env_state, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        elif variant == "streaming_python":
            dump_mapping_streaming(env_state, f, use_libyaml=False)
        elif variant == "streaming_libyaml":
            dump_mapping_streaming(env_state, f, use_libyaml=True)
        else:
            raise SystemExit(f"unknown variant {variant!r}")
    elapsed = time.perf_counter() - started
    peak = _rss_bytes()
    return {
        "variant": variant,
        "state_json_mib": round(state_json_bytes / 2**20, 1),
        "output_mib": round(out.stat().st_size / 2**20, 1),
        "wall_s": round(elapsed, 2),
        "rss_at_rest_mib": round(at_rest / 2**20),
        "rss_peak_mib": round(peak / 2**20),
        "write_cost_mib": round(max(0, peak - at_rest) / 2**20),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mb", type=float, default=100.0, help="JSON size of the state (MiB)")
    parser.add_argument("--variant", choices=VARIANTS, action="append")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.child:
        (variant,) = args.variant
        with tempfile.TemporaryDirectory(prefix="bench-final-state-") as tmp:
            print(json.dumps(_run_variant(variant, args.mb, Path(tmp))))
        return 0

    for variant in args.variant or VARIANTS:
        proc = subprocess.run(
            [sys.executable, __file__, "--child", "--mb", str(args.mb), "--variant", variant],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"{variant}: failed\n{proc.stderr}", file=sys.stderr)
            return proc.returncode
        print(proc.stdout.strip(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
