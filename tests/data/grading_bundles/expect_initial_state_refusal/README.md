# `expect_initial_state_refusal`

**This bundle was authored, not recorded.** No agent ran this trial and no runner
produced this grade; the files were written by hand to carry one grading shape into a
regression pin. That is why it sits here rather than under a project's
`output/trials/` path, and why it is called a bundle rather than a trial.

What it holds is what a recorded bundle holds — a grading config, the state the task
starts in, a trajectory, the final environment state and a golden `grade.yaml` — in the
shape a refusal task declares: `state_checks.hash` enabled with `expect_initial_state`
as its source, meaning the expected final state *is* the initial state.

The verdict in `grade.yaml` is derivable by hand rather than transcribed from a run:
`env.yaml`'s `db` block is byte-identical to `bundle.yaml`'s `initial_state.json_db`, so
the hash comparison matches and scores `1.0`; `state_checks` is the only weighted
component, so the trial scores `1.0` and clears its `pass_threshold` of `1.0`. What the
pin locks is that core's grading of this config keeps producing that verdict, reason
strings included.

Read by `tests/canonical/test_golden_trajectory_grading_canon.py`.
