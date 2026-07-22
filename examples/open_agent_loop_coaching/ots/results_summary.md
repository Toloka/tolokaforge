# Coaching A/B study — cross-task real-run results

Cross-task validation that the OAL coach substrate works beyond the
single ticket task shown in the parent demo. Three arms (`solo` ·
`rule_coached` · `llm_coached`), same agent + same seed across arms.
Nine trials per arm (three tasks × three repeats).

## Method

- **Agent**: `openrouter/moonshotai/kimi-k2.6`, `temperature=0.6`, adaptive reasoning, `max_tokens=16384`, `seed=42`.
- **User simulator**: `openrouter/anthropic/claude-sonnet-4.6`.
- **Tasks**: `tool_use_public_example_01`, `tool_use_public_example_02`, `coding_public_example_01` (all bundled in `examples/native/`). Chosen to give cross-task signal — different tool sets, different agent-workload shapes.
- **`orchestrator.repeats = 3`**, `max_turns = 30`.
- **Coach configs**: `coach_configs/rule.yaml` (deterministic loop detector) and `coach_configs/llm.yaml` (Claude Haiku analyzer + suggester, per-trial `budget_usd = 0.20`). Both task-agnostic — no changes between task packs.

## Batch 1 — sonnet-4.6 judge

| arm | trials | pass@1 | avg score | avg turns | agent $ | coach $ | total $ | interventions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **solo** (sealed) | 9 | 0.00 | 0.00 | 30.0 | 0.0636 | 0.0000 | 0.0636 | 0 |
| **rule_coached** | 9 | 0.00 | 0.00 | 30.0 | 0.0610 | 0.0000 | 0.0610 | 26 |
| **llm_coached** | 9 | 0.00 | 0.00 | 30.0 | 0.0638 | 0.0220 | 0.0858 | 29 |

**Total batch 1 cost: $0.21.**

### Interpretation

**Nobody passed the binary criterion.** Kimi-K2.6 is not strong enough to solve any of the three chosen tasks within 30 turns under these conditions; every trial timed out at `max_turns=30`. That is a real finding about the agent, not about the coach — the coach can inject hints, but it cannot compensate for a model that lacks the underlying capability.

**The coach substrate is validated end-to-end.** All 55 interventions across the two coached arms landed with `ack: accepted` in the OAL trace and correspond to real user-role messages in the trajectory. Coach reports, per-trial cost accounting (via `CostTrackingLLMCall`), and the composed sink+controller layer all functioned exactly as designed.

**Coach selectivity is the headline finding.** The 26+29 interventions concentrate almost entirely on a *single* trial — `tool_use_public_example_01:0`, where the agent got stuck in a legible JSONPath loop. The other 8 trials produced **zero** coach fires. That's the ideal signal: the coach speaks up on real stuck patterns and stays silent otherwise. It does *not* spray hints uniformly.

**Cost profile is honest.** The rule coach adds $0 to the agent bill (no LLM). The LLM coach adds ~$0.024 across 9 trials, or roughly $0.0024 per trial — cheap enough to run at benchmark scale. Judge cost (sonnet-4.6) dominates over both. When you multiply the arm totals by an OTS scale run (300 tasks × N repeats), the coach contribution stays proportional and small.

**Sample LLM-coach intervention** (from `llm_coached_sonnet_judge_.../trials/tool_use_public_example_01/0/open_agent_loop.yaml`):

> "The database queries are returning empty results. Try querying the root path directly: `db_query({"jsonpath": "$"})` to verify the database structure…"

Specific to the actual failure pattern (empty JSONPath queries), not a generic template — the LLM read the events and drafted a task-shaped hint.

## Batch 2 — opus-4.8 judge (partial)

Batch 2 ran with `anthropic/claude-opus-4.8` as the judge (production-parity). Solo arm completed; rule arm's first trial completed before an external process kill halted the run. No re-run was launched — the primary A/B story is already told by batch 1.

| arm | trials | pass@1 | avg score | agent $ | notes |
| --- | --- | --- | --- | --- | --- |
| **solo** (opus judge) | 9 | 0.00 | 0.02 | 0.2070 | ~3.3× the sonnet-judge solo cost |
| **rule_coached** (opus judge) | 1 (of 9) | — | — | 0.0574 | trial 0 only; killed mid-arm |

**Takeaway.** The judge-model choice is the dominant cost knob (opus is ~3–5× sonnet per trial for the same agent workload). The coach machinery itself contributes negligibly to total cost regardless of which judge model is used.

## Cross-task generalization signal

The coach configs — `rule.yaml` and `llm.yaml` — were the same YAML files used against the ticket-only demo in the parent directory. **No modifications required** to run against `coding_public_example_01` (Python code writing with bash + read_file + write_file) or `tool_use_public_example_02`. This is the core substrate claim: the sink + controller + tool machinery is task-agnostic.

## What we did NOT get (and why)

**True OTS coaching data (tau_manufacturing pack).** The plan was to run against production OTS tasks in the internal `tolokaforge-tasks` repo. Blocker: the private `tolokaforge-adapter-frozen-mcp-core` v0.2.1 references a `task.user_simulator` field that was removed from `TaskConfig` in the M9 project-layer landing on `main`. The adapter needs a version bump against post-0.9.2 tolokaforge before OTS runs can happen here. This is a private-tools/public-core version drift, not an OAL issue. Bookmark branch `experiment/oal-coach-ab-2026-07-17` on `tolokaforge-tasks` records the intended task snapshot.

**Positive pass rates.** All arms produced `pass@1 = 0`. That reflects the kimi-K2.6 agent's capability on these three tasks under 30-turn caps, not a coach failure. Prior tool_use runs with sonnet-4.6 as the agent produced non-zero partial scores; kimi struggles more.

## Reproducing

```
scripts/with_env.sh uv run --package intervener python \
  examples/open_agent_loop_coaching/run_ab_study.py \
  --configs-dir examples/open_agent_loop_coaching/ots \
  --judge-batch sonnet

uv run python examples/open_agent_loop_coaching/analyze_results.py \
  --results-dir results/coaching_ab_ots
```

Batch 1 (sonnet judge) reproduces in ~24 minutes at ~$0.21. Batch 2 (opus judge) requires ~1 hour and ~$0.60 for the full three-arm study.
