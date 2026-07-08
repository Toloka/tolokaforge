# Dimension: consistency-passk

Prepend `_shared_context.md`. Requires the FULL-EVAL layout (scored `grade.yaml`).

Compute, per-domain and micro (micro base defined in the shared block: pool ALL tasks
across domains, task-count-weighted):
- pass@1, pass@5 (ceiling), pass^5 (board) per the shared-block formulas. Remember pass^5
  is per-task `success_rate**5` averaged, NOT the engine `pass_hat@5` (which is the ceiling).
- consistency tax = pass@5 - pass@1, computed over the SAME task set for both (the n>=5
  subset; tasks with n<5 are excluded from pass@5/pass^5, so report the tax over that subset
  or note the mismatch).
- classify each TASK: SOLID (pass_rate == 1), FLAKY (0 < pass_rate < 1), HARD
  (pass_rate == 0); count each per domain. FLAKY share = the consistency-limited
  opportunity; HARD share = the capability floor.

MODE: EVAL only for the board numbers (needs scored per-task c/n). In OBSERVE mode there is no
board pass^k; degrade to a per-PROBE band over the K repeats (`findings.json` `per_probe`
passed/runs: SOLID / FLAKY / HARD), which surfaces the flaky-vs-genuine split the resolve
stage cares about.

Method: prefer `per_task_metrics.json` (per-task rows: `success_rate` = c/n gives pass@1
directly, `success_rate**5` gives pass^5); `aggregate.json` holds ONLY run-level rollups,
not per-task stats. Fallback: ONE python pass over `grade.yaml` (`binary_pass` is a
grep-able top-line boolean), grouping trials by TASK dir for n and c. If any task has n < 5,
note it. Cross-check the micro numbers against `{{ANCHORS}}` and flag any drift.

RETURN (compact markdown):
- per-domain table: pass@1 / pass@5 / pass^5 / tax / #SOLID / #FLAKY / #HARD
- micro row; match-vs-anchors (yes/no per number)
- VERDICT: CONSISTENCY-limited or CAPABILITY-limited, decided numerically: compare the
  consistency-recoverable pp (the FLAKY tasks' pass@5 - pass@1 contribution) against the
  HARD-task pp; label CAPABILITY-limited if HARD pp >= consistency pp, else CONSISTENCY-
  limited. Report BOTH numbers; call out any single capability-floor domain.
