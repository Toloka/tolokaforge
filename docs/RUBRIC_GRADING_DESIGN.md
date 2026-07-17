# Rubric Grading — Design

Status: implemented. This is the as-built architecture and rationale. For the
authoring/usage reference see [GRADING.md](GRADING.md) and the `grading.yaml`
schema in [REFERENCE.md](REFERENCE.md).

## Motivation

Rubric grading was nominally available via an "LLM judge" but did not work. The
concrete defects:

1. **Two divergent judge implementations, neither owned.** A rich agentic
   `LLMJudge` reachable only through `GradingEngine` / `BaseAdapter.grade()` —
   which had **no production caller** (dead code) — and a minimal
   `evaluate_llm_judge` on the live gRPC `GradeTrial` path that did a single
   completion returning a flat `{score, reasons}`.
2. **Unstructured rubric.** `rubric: str` (a free-text blob) → a single scalar.
   No per-criterion scoring, no diagnostics, no partial-credit gradient.
3. **A required-but-ignored `output_schema`** field the live judge never read.
4. **The proto contradicted the code** ("LLM judge NOT computed by Runner" while
   the runner computed it).
5. **Silent score fallbacks** on judge malfunction (0.0 / 0.5), masking errors.

## Architecture

Grading runs in the **runner** (gRPC `GradeTrial`), co-located with the state
service (`DBServiceClient`) and the trial workspace. The rubric judge is one
component of `combine_grade_components`, alongside state checks and transcript
rules; its aggregated score feeds the existing `weights.llm_judge` slot.

```
orchestrator ──RegisterTrial──▶ runner            (TaskDescription incl. grading.llm_judge.rubric)
orchestrator ──GradeTrial─────▶ runner._grade_trial_async
                                  ├─ state checks / transcript rules
                                  └─ rubric judge:  LLMJudge(...).run(...)
                                        ├─ LLMClient(judge_model_config) via build_capabilities  (run-level models.judge)
                                        ├─ ToolCallingLoop (no user sim; stop on submit_report; max_turns + wall-time)
                                        │     read-only tools: get_db_state/query_db, search_kb, read_file, submit_report
                                        ├─ parse_submit_report  → list[CriterionResult]   (fail loud)
                                        └─ aggregate_rubric      → score + gate
                                  └─ Grade{ binary_pass, score, components, criterion_results,
                                            judge_status, judge_report(usage+transcript) }
orchestrator ◀──GradeTrialResponse── proto Grade ── shared_stack_runtime ── Pydantic Grade ── TrialArtifactWriter
                                                                                          grade.yaml + judge_trajectory.yaml
```

The judge runs on the **same `ToolCallingLoop`** the agent uses
(`core/loop.py`), configured with no user simulator and a termination policy
that stops the moment `submit_report` appears in the tool calls. The loop is
sync; the async runner bridges it with `run_in_executor`, and DB-read tools
bridge back to the runner event loop with `run_coroutine_threadsafe(...)`.

## Key components

| Component | Location | Role |
|---|---|---|
| `ToolCallingLoop` | `core/loop.py` | Shared multi-turn tool-calling engine (agent + judge) |
| `Rubric` / `Criterion` | `runner/models.py` | Author-facing config (Pydantic, `extra=forbid`) |
| `CriterionResult` | `runner/models.py` | Per-criterion verdict (output side) |
| `build_submit_report_tool` | `core/grading/rubric.py` | Generates the terminal tool schema from the rubric |
| `parse_submit_report` | `core/grading/rubric.py` | Validates judge output → `CriterionResult` (fail loud) |
| `aggregate_rubric` | `core/grading/rubric.py` | Weighted score + required-gate |
| `Judge` / `LLMJudge` | `core/grading/judge.py` | `Judge` Protocol (grading-plane seam); `LLMJudge` builds the client + read-only tools and runs the loop |
| read-only judge tools | `core/grading/judge_tools.py` | `get_db_state`/`query_db`/`search_kb`/`read_file` |
| `JudgeStatus` / `JudgeReport` | proto + `core/models.py` | Errored status + judge usage/transcript |
| calibrator | `tools/rubric-calibrator` | Agreement metrics + trust gate over golden fixtures |

## Design decisions

| Decision | Rationale |
|---|---|
| **Break `rubric: str` compat** | Rubric grading never worked end-to-end; no in-tree task used it. |
| **Judge runs in the runner** | DB + workspace live there; matches what the live code already did. Concurrency handled by raising the `GradeTrial` timeout (300→600s) + a per-judge `max_turns`/wall-time budget, not by moving host-side. |
| **One shared loop** | The judge is "the agent loop run as a solo grader." Avoids a third near-duplicate loop; the old `_grade_agentic` and `GradingEngine` judge path were deleted. |
| **Harness-owned read-only tools** (not the agent's tools filtered by `category`) | `category` is stamped `"compute"` for all native task tools, so a filter excludes nothing — untrustworthy. A fixed read-only allowlist is safe and modality-correct without a DB clone. |
| **Run-level judge model via `build_capabilities`** | An agentic tool-calling judge needs the preset/policy machinery (schema sanitizers, reasoning codecs) — raw `litellm.completion` mangles tool calls for Gemini/GPT-5/etc. The model is a run-level role (`RunConfig.models["judge"]`, carried on `TrialSpec.judge_model_config`), separate from the agent under test to avoid self-grading bias, with no default and no fallback — a fleet-wide provider switch is a one-line run-config edit. |
| **Fail loud → `JudgeStatus.ERRORED`** | Any judge malfunction (no `submit_report`, retry exhaustion, budget/turn exhaustion, crash) yields ERRORED with **no score**; the `llm_judge` component stays at the `-1.0` sentinel (excluded from the combine), never 0.0/0.5. |
| **Structured output via `submit_report`** | The tool's arg schema is generated from the rubric and validated fail-loud (bounded re-prompt, then ERRORED). The rejection is returned to the judge as the `submit_report` call's **tool result** (every `tool_call_id` on the terminating turn answered by an adjacent `role=tool` result), so the re-prompt is a provider-valid tool-call/tool-result cycle. No MCP — the loop is in-process. A flat arg object (per-criterion keyed by id) avoids nested-schema dialect gaps. |
| **Justification before verdict + consistency marker** | Each criterion's `<id>_justification` field is emitted **before** its verdict field (`met` / `score`) in both `properties` order and the `required` list: providers generate tool arguments in schema order, so the verdict token is produced *conditioned on* the written reasoning (reason-then-answer / autoregressive generation), not thousands of tokens before it. Because JSON-Schema property order is non-normative, a backstop rides on top: every justification must end with a `VERDICT: MET` / `VERDICT: NOT MET` (binary) or `SCORE: <value>` (graded) marker line, and `parse_submit_report` rejects any call whose trailing marker is missing or disagrees with the submitted verdict — a `VerdictConsistencyError` that rides the same bounded re-prompt to ERRORED on exhaustion. The marker requirement is stated on **both** surfaces the judge sees — the per-field schema descriptions (the machine contract) and one sentence in the judge system prompt — so a provider that truncates or reorders schema descriptions still gets the instruction. The marker is matched against the justification's final non-empty line, taking the last `VERDICT:` / `SCORE:` occurrence, so real models may append it inline to the closing sentence. |
| **Author-written reference channel** (`rubric.reference`, `criterion.expected`) | The judge needs the correct answer to grade reference-dependent criteria — but it is given an author-written reference, **not** the deterministic oracle (`golden_actions`/`expected_hash`/`jsonpath_checks`), which would bias toward the golden path and double-count state checks. |
| **Narrow input surface** | The `Judge` Protocol's `run()` accepts only `{agent_system_prompt, transcript, rubric, read-tools, state_diff}` — oracle fields (`golden_actions`/`expected_hash`/`jsonpath_checks`/`grading_config`) cannot leak because they are not on the surface. Agent system prompts are policy-only by convention. |
| **Calibration gates trust** | A rubric is not trustworthy until it clears an agreement threshold against human-labeled fixtures. |

## Scoring semantics

A criterion is `binary` (judge reports `met`) or `graded` (judge reports a
`score` in [0,1]); a graded criterion's `met` is derived at the 0.5 threshold for
gating only. The judge writes each criterion's justification first, then its
verdict, and ends the justification with a `VERDICT: MET` / `VERDICT: NOT MET`
(binary) or `SCORE: <value>` (graded) marker line.

- **Verdict/justification consistency:** the submitted verdict must agree with
  the justification's trailing marker — the marker is read from the final
  non-empty line only (whitespace-tolerant, case-insensitive; `NOT MET` matched
  ahead of `MET`), so reasoning that discusses "NOT MET" mid-text cannot
  false-match. For a graded criterion the marker value must be within `0.05` of
  the submitted `score`. A missing / unparseable / contradicting marker is a
  hard reject (unverifiable is not acceptable) that rides the bounded re-prompt
  to ERRORED on exhaustion — never a silently wrong grade. The marker is kept
  **verbatim** in `criterion_results[].justification` for audit fidelity.

- **Required-gate:** any `required` criterion with `met=false` fails the rubric
  outright (`gate_failed`) — `binary_pass=false` and the `llm_judge` component is
  forced to 0.0, **independent of `pass_threshold`**. A high weighted score
  cannot rescue a failed required criterion. Per-criterion detail survives in
  `criterion_results` regardless.
- **Weighted score:** `Σ(weight·score) / Σ(weight)` over **non-required**
  criteria only (required criteria are pure gates, not weighted contributors). An
  all-required rubric scores 1.0 when the gate passes, 0.0 when it fails.
- **Two weighting layers:** per-criterion `weight` (inside the rubric) composes
  multiplicatively with the top-level `weights.llm_judge` that scales the whole
  judge component in `combine_grade_components`.

## Output

Per trial (`docs/OUTPUT_FORMAT.md`):

- `grade.yaml` — `binary_pass`, `score`, `components`, `criterion_results`
  (id/met/score/justification), `judge_status`, `judge_usage`
  (calls/tokens/cost/tool_calls).
- `judge_trajectory.yaml` — the judge's own message transcript (tool calls +
  `submit_report`), written only when present. This is the audit/reproducibility
  channel; the agentic judge is not bit-reproducible even at temperature 0.
- `judge_inputs.yaml` — the judge's non-derivable `run()` inputs (the `state_diff`
  string it saw + its non-KB read-tool surface), written only when a judge ran.
  This is what offline replay reads to re-execute the judge without live services.

## Replay

`tolokaforge rejudge` re-executes only the rubric-judge stage over a recorded run,
reconstructing `LLMJudge.run()` inputs from the bundle (transcript, agent policy,
rubric, judge model, `state_diff`) and re-running the *same* `LLMJudge` — a caller,
not a second judge. Live read tools become offline shims that return an explicit
"unavailable in replay" marker. It exists to validate a judge change (schema,
prompt, wording, model) against recorded trajectories with judge-only spend. See
[`docs/JUDGE_REPLAY.md`](JUDGE_REPLAY.md).

## Calibration

`tools/rubric-calibrator` runs the real judge over golden fixtures
(`{rubric, transcript, final_db_state?, workspace?, expected per-criterion}`),
computes per-criterion accuracy + Cohen's κ (graded scores binarised at 0.5),
lists disagreements, and applies a **trust gate**: it exits non-zero when overall
agreement is below threshold OR any fixture errored. Integration is opt-in
(default-skipped) so a bare test run never spends money.

## Known limitations / future work

- **Office/PDF judge readers not restored.** The new judge offers only
  `read_file` (UTF-8 text); the old (dead) judge had xlsx/docx/pptx/pdf readers.
  Restore as a read-only allowlist before any office-deliverable task lands.
- **Graded calibration is binarised** at 0.5 — agreement metrics do not catch
  graded-magnitude drift (a future per-graded MAE metric would).
- **Single-call grading** risks cross-criterion anchoring; per-criterion calls
  remain a fallback if calibration shows bias.
- **Two `RequiredAction`/`TranscriptRulesConfig` schemas** (host `core/models.py`
  vs runner `runner/models.py`) could be consolidated.
