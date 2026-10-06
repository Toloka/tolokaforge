# Harbor (Terminal-Bench 2.0) task pack

A vendored Terminal-Bench 2.0 task: a `task.toml`, an `environment/` build
context, and a `tests/test.sh` that writes a reward float to
`/logs/verifier/reward.txt`.

| Task | Difficulty | Stack | Goal |
|---|---|---|---|
| [`write-release-note`](write-release-note/) | easy | Python 3.11 slim | write an exact release note the verifier checks |

`write-release-note` is a small self-contained task: the agent is given a single
`bash` tool scoped to the task container, and the verifier scores 1.0 when
`/app/RELEASE_NOTE.txt` holds exactly the required line. The pack also ships a
`write-release-note/solution/solve.sh` reference solution, so Harbor's keyless
`oracle` agent can solve it with no provider key (reward 1.0).

## Two ways to run it

- **Through the Harbor harness** (`harbor` adapter — delegation): the trial's
  agent step is a single `harbor run`, and Harbor drives its own Terminus 2 agent
  + verifier. Run configs here:
  - [`run_harbor.yaml`](run_harbor.yaml) — a single true-Harbor run (with a
    keyless `oracle` variant in its header for a $0 smoke).
  - [`run_harbor_multi.yaml`](run_harbor_multi.yaml) — a `harbor` leg beside a
    `native` leg, to surface the per-harness comparison report ("our loop" vs the
    Harbor harness).

  Needs `pip install "tolokaforge[harbor]"`, a running Docker daemon, and
  `--image-source build`. See the package
  [`README.md`](../../external_adapters/tolokaforge-adapter-harbor/README.md).

- **On tolokaforge's own loop** (`terminal_bench` adapter — task-reuse):
  Terminal-Bench 2.0 is the on-disk shape the shipped `terminal_bench` adapter
  already discovers and parses (`task.toml` + `environment/` + `tests/`), so this
  pack is a valid terminal-bench pack — point a terminal-bench run's
  `terminal_bench_dir` at this directory to run it. See
  [`docs/ADAPTERS.md`](../../docs/ADAPTERS.md) and
  [`examples/terminal_bench/`](../terminal_bench/).
