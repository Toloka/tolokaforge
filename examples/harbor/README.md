# Harbor (Terminal-Bench 2.0) task pack

A vendored Terminal-Bench 2.0 task: a `task.toml`, an `environment/` build
context, and a `tests/test.sh` that writes a reward float to
`/logs/verifier/reward.txt`.

| Task | Difficulty | Stack | Goal |
|---|---|---|---|
| [`write-release-note`](write-release-note/) | easy | Python 3.11 slim | write an exact release note the verifier checks |

`write-release-note` is a small self-contained task: the agent is given a single
`bash` tool scoped to the task container, and the verifier scores 1.0 when
`/app/RELEASE_NOTE.txt` holds exactly the required line.

Terminal-Bench 2.0 is the on-disk shape the shipped `terminal_bench` adapter
already discovers and parses (`task.toml` + `environment/` + `tests/`), so this
pack is a valid terminal-bench pack — point a terminal-bench run's
`terminal_bench_dir` at this directory to run it. See
[`docs/ADAPTERS.md`](../../docs/ADAPTERS.md) and
[`examples/terminal_bench/`](../terminal_bench/).
