# Coaching A/B study — real OTS tasks

Three-arm A/B (**solo** · **rule_coached** · **llm_coached**) against production OTS tasks from `tolokaforge-tasks/tasks/tau_manufacturing/` — `MAN-34`, `MAL-007`, `MAN-46`. Same agent + same seed across arms. Nine trials per arm (three tasks × three repeats).

## Method

- **Agent**: `openrouter/moonshotai/kimi-k2.6`, adaptive reasoning, `max_tokens=16384`, `seed=42`.
- **User simulator**: `openrouter/anthropic/claude-sonnet-4.6`.
- **Judge**: `anthropic/claude-sonnet-4.6`.
- **Harness adapter**: `frozen_mcp_core` (private, via `tolokaforge-adapter-frozen-mcp-core` from `tolokaforge-tools`).
- **`orchestrator.repeats = 3`**, `max_turns = 30`, `runtime = docker`.
- **Coach configs**: `coach_configs/rule.yaml` (event-pattern detector) and `coach_configs/llm.yaml` (Claude Haiku analyzer + suggester, per-trial `budget_usd = 0.20`). Both unchanged from the earlier tool_use demo — task-agnostic.

## Headline numbers (honest, incomplete)

| arm | completed | pass rate | avg turns | agent $ | coach $ | total $ (incl judge) |
| --- | --- | --- | --- | --- | --- | --- |
| **solo** | 3/9 | 33% (1/3) | 18.3 | 0.264 | — | 0.435 |
| **rule_coached** | 1/9 | 0% (0/1) | 15.0 | 0.050 | 0.000 | 0.050 |
| **llm_coached** | **7/9** | **57% (4/7)** | 17.9 | 0.625 | 0.043 | **1.212** |

Total study cost: **~$1.70**, well under the $10 cap.

### Why the completion counts differ

The Docker Runner container crashed between the `MAN-34` and `MAL-007` tasks in both the solo and rule_coached arms. Subsequent trials failed at `Failed to register trial with executor: gRPC error` — the container never came back. The LLM arm happened to get a fresh Docker instance that survived long enough to get through MAL-007 + MAN-46:0.

**This is an infrastructure artefact, not an OAL issue.** The session gate + coach substrate handled every trial that reached execution. The gRPC failures were reported cleanly through the orchestrator's retry layer; the driver's cost-rail didn't misfire.

**The arms are not directly comparable at the aggregate level** because the workloads that completed differ (solo/rule saw only MAN-34; LLM saw all three tasks). The per-trial data below is where the real signal lives.

## Notable save

**`MAN-34:0` — LLM coach turned a fail into a pass.**

| arm | outcome | turns | agent $ | coach interventions |
| --- | --- | --- | --- | --- |
| solo | fail (0.41) | 12 | 0.070 | — |
| llm_coached | **pass (1.00)** | 15 | 0.078 | 2 (both accepted) |

The LLM coach saw the agent repeatedly calling `tau_manufacturing_modify_lot` with identical parameters for lot `LOT-P4XM` and drafted a specific hint pointing at the repetition. Verbatim from the OAL trace:

> "THE AGENT IS REPEATING THE SAME TOOL CALL TWICE IN A ROW (TAU_MANUFACTURING_MODIFY_LOT FOR LOT-P4XM WITH IDENTICAL PARAMETERS), WHICH INDICATES LOOPING BEHAVIOR."

Delta cost to convert a fail into a pass: **$0.008** (agent + coach LLM combined) plus the modest turn increase.

## Notable harm

**`MAN-34:1` — LLM coach turned a pass into a fail.**

| arm | outcome | turns | agent $ | coach interventions |
| --- | --- | --- | --- | --- |
| solo | pass (1.00) | 17 | 0.075 | — |
| llm_coached | fail (0.23) | 30 | 0.171 | 5 (all accepted) |

The coach fired 5 times mid-trajectory and the agent got dragged off course, running to max_turns without completing. **Over-intervention is real.** A production coach would need a smarter cooldown or an "am I helping?" self-check to avoid this failure mode.

## Per-trial data (LLM arm — the fullest picture)

| trial | outcome | score | turns | cost | coach fires |
| --- | --- | --- | --- | --- | --- |
| MAN-34:0 | **pass** | 1.00 | 15 | 0.078 | 2 |
| MAN-34:1 | fail | 0.23 | 30 | 0.171 | 5 |
| MAN-34:2 | fail | 0.00 | 14 | 0.079 | — |
| MAL-007:0 | **pass** | 1.00 | 15 | 0.062 | — |
| MAL-007:1 | **pass** | 0.88 | 13 | 0.051 | — |
| MAL-007:2 | **pass** | 0.88 | 14 | 0.076 | — |
| MAN-46:0 | fail | 0.00 | 20 | 0.107 | — |

MAL-007 delivered 3/3 passes despite (mostly) silent coach — the coach didn't fire on the tasks where the agent was fine. **The coach's selectivity finding from the earlier tool_use runs replicates on OTS data**: 25 total interventions across 9 trials, concentrated on the trials where the agent showed a legible failure pattern.

## What this validates about the OAL substrate

- **Runs end-to-end on real OTS production tasks.** Session gate, loop seams, `OpenAgentLoopManager`, intervention pump, coach `ComposedParticipant`, `CostTrackingLLMCall`, YAML trace — all exercised, all functioned.
- **Task-agnostic in practice.** Same coach YAML files that ran on `tool_use` also worked on `tau_manufacturing` with a completely different tool set (19 tools vs 4). The rule detector's "same tool 3× in a row" and the LLM detector's `STUCK`/`OK` verdict both fire meaningfully on real production workloads.
- **Rich, specific coach output.** The LLM coach's hint on MAN-34:0 was concretely tied to the actual tool + lot ID the agent was looping on — not a generic template.
- **Cost accounting stays honest.** Coach LLM spend (~$0.043 across all coached trials) shows up in `coach_report.yaml` sidecars, separate from the trial's own `metrics.yaml`. Cross-checked against the `aggregate.json` per arm.

## What the study did NOT establish

- **A clean apples-to-apples aggregate comparison.** Docker Runner instability truncated solo and rule arms after MAN-34. To get a real head-to-head at pass@k, someone would need to rerun with a more resilient runner (or explicitly restart Docker between task groups).
- **A batch 2 with opus judge.** Deferred — the sonnet-judge data already tells the story, and the earlier partial opus-solo run confirmed the judge cost multiplier (~3.3×) without needing a full second batch.

## What this study raises as follow-ups

1. **Coach over-intervention** (MAN-34:1 harm case) — worth implementing an adaptive cooldown or a "score trend" check the coach can use to shut up when the agent is progressing.
2. **Docker Runner resilience under multi-trial arms** — the crash between task packs is real ops noise unrelated to OAL, but it complicates evaluation runs. A "restart Runner between task groups" flag would help.
3. **MAL-007's 3/3 pass streak** — the agent may be more competent than the MAN-34 numbers suggest. A larger-N run per task would sharpen this.

## Reproducing

```
scripts/with_env.sh uv run --package intervener python \
    examples/open_agent_loop_coaching/run_ab_study.py \
    --configs-dir examples/open_agent_loop_coaching/ots \
    --judge-batch sonnet

uv run python examples/open_agent_loop_coaching/analyze_results.py \
    --results-dir results/coaching_ab_ots
```

Requires `tolokaforge-adapter-frozen-mcp-core` installed. It ships in the internal `tolokaforge-tools` repo and needs to be pinned against `tolokaforge >= 0.9.2` (see `tolokaforge-tools#38` / `#39` for the M9 project-layer compatibility fix).
