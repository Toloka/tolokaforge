# rubric-calibrator

Stage 6 of the rubric-grading work (`plans/rubric_grading.md`). A rubric judge is
**not trustworthy until it clears an agreement gate**. This tool runs the real
runner-side judge (`tolokaforge.core.grading.judge.run_rubric_judge`) over
human-labelled golden fixtures, measures per-criterion agreement, and applies a
trust gate that blocks shipping an under-agreeing rubric.

## What it does

1. Loads golden fixtures (rubric + agent transcript + optional final DB state +
   optional workspace + human per-criterion labels).
2. Runs the real judge on each fixture, using an in-memory jsonpath-evaluating
   `DBReader` (the Stage-4 live-test pattern) so no runner / gRPC stack is needed.
3. Compares the judge's verdicts against the human labels and computes
   per-criterion **accuracy** and **Cohen's κ** (chance-corrected), aggregated
   across fixtures, plus a list of disagreements (with the judge's justification).
4. Surfaces total judge token usage / cost.
5. Applies the **trust gate**: exits non-zero ("not shippable") when overall
   agreement is below the threshold OR any fixture's judge run errored.

An ERRORED judge run is a calibration **failure**, never a silent score.

**Graded criteria are calibrated as binarised met/not-met** at the 0.5 threshold
(both the judge's `score` and the human's `score` are thresholded), so accuracy
and Cohen's κ measure label agreement, not score magnitude. The gate therefore
does **not** catch graded-magnitude drift (e.g. judge=0.6 vs human=0.9 both count
as "met" → agreement). A future per-graded MAE metric could close that gap.

## Usage

```bash
# Via the bundled wrapper (loads .env for provider keys):
scripts/analysis/calibrate_rubric.sh tools/rubric-calibrator/fixtures \
    --threshold 0.6 --metric kappa

# Or directly:
scripts/with_env.sh uv run rubric-calibrator \
    tools/rubric-calibrator/fixtures --model-ref openrouter/openai/gpt-4.1-mini
```

Options: `--model-ref/-m` (judge model, default a cheap small model),
`--threshold/-t` (minimum agreement), `--metric` (`kappa` | `accuracy`),
`--max-turns`. Exit code 1 = trust gate failed; 2 = bad fixtures / args.

## Fixtures

Golden fixtures live under `fixtures/` as YAML (they ship as calibration assets
with the tool, not as test snapshots). See `fixtures/refund_partial_credit.yaml`
for the schema: `id`, `rubric`, `agent_system_prompt`, `transcript`,
`final_db_state` (optional), `workspace` (optional), `rag_url` (optional), and
`expected` (one human label per criterion — `met` for binary, `score` for graded).

## Layout

- `src/rubric_calibrator/metrics.py` — pure agreement maths (no LLM, unit-tested).
- `src/rubric_calibrator/fixture.py` — fixture schema + loader (Pydantic v2).
- `src/rubric_calibrator/runner.py` — drives the real judge, pairs verdicts.
- `src/rubric_calibrator/report.py` — rich report rendering.
- `src/rubric_calibrator/cli.py` — typer CLI + trust gate.

## Tests

```bash
cd tools/rubric-calibrator && uv run pytest tests/ -m unit          # pure + scripted-harness
cd tools/rubric-calibrator && scripts/with_env.sh uv run pytest tests/ -m integration  # real LLM
```
