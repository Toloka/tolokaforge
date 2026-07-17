# Rubric judge verdict-consistency fixtures — FROZEN HISTORICAL CAPTURES

`ae_bdg_002_1_submit_report.json` and `ae_bdg_003_0_submit_report.json` are the
verbatim per-criterion `submit_report` payloads from two trials of GitHub Actions
run **29240785466** (2026-07-13), judge `anthropic/claude-opus-4.8` @ temp 0.0,
tolokaforge **v0.7.0**. In both, the `no_internal_references` criterion was
submitted `met: false` while its justification concludes the criterion **is**
met — the exact verdict/justification contradiction that motivates the
verdict-consistency check.

**These are frozen. Do not regenerate them and never re-canonize them.** The flip
is a rare, wording-dependent event (0/570 in the replay experiment); regenerating
would erase the real-world contradiction these fixtures pin. They are not produced
by any test and have no `--update-canon` path.

Each file is the argument dict the judge passed to `submit_report`: for every
criterion an `<id>` verdict and an `<id>_justification`, plus overall `reasons`.
The justification text is the verbatim v0.7.0 capture and carries **no** trailing
`VERDICT:` / `SCORE:` marker — the marker contract did not exist in v0.7.0.

Two rejection shapes are exercised in `test_rubric.py`:

- **(a) raw recorded payload** — no marker → rejected for a **missing marker**.
- **(b) real justification + reconstructed marker** — the verbatim v0.7.0
  justification with a `VERDICT: MET` line appended while the boolean stays
  `false` → rejected for **contradiction**. The justification is real (its text
  concludes the criterion IS met); the appended `VERDICT: MET` is exactly what
  the fixed judge would emit. No *real* marker-contradiction payload can exist
  until the new schema ships, so the marker is reconstructed, not synthetic.
