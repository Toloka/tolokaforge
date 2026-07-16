# Open Agent Loop — coaching A/B study

**A configurable coach participant attaches to a running trial, watches
the agent, and helps it when it gets stuck. This example runs the same
task under three configurations (solo · rule-coached · LLM-coached) and
compares the results.**

The whole point of the OAL gate is that it lets non-agent code — a
coach, a safety monitor, an observability sink, a human — attach to a
live trial. This example turns that plumbing into a measurable claim:
does mid-trial coaching improve a benchmark, and at what cost?

Related docs:

- [`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md) — the gate itself.
- [`docs/INTERVENER.md`](../../docs/INTERVENER.md) — the peer package the coach is built on.
- [ADR-0019](../../docs/architecture/adr/0019-open-agent-loop-sessions.md) — architectural record.
- [`examples/open_agent_loop/`](../open_agent_loop/) — the simpler LLM copilot demo.

---

## The setup

- **Task:** `tool_use_public_example_01` "Ticket Resolution Plan". The
  agent must query a JSON DB, read a policy file, and update a ticket.
  Empirically, agents *reliably fail* here — 12 of 13 recent human trials
  hit `max_turns` because the agent loops on JSONPath queries returning
  `[]` when the target record actually exists in the initial state. This
  makes it a legible failure mode a coach can catch.
- **Model:** OpenRouter → `anthropic/claude-sonnet-4.6`, fixed seed=42,
  same across all arms. Any pass@k difference is attributable to the
  coach and nothing else.
- **Repeats:** 4 by default (change `orchestrator.repeats` in the run
  configs to scale up).

## The three arms

| Arm | Coach config | What it does |
| --- | --- | --- |
| **`solo`** | none (`open_agent_loop.enabled: false`) | Sealed baseline. Byte-identical to a pre-OAL run. |
| **`rule_coached`** | [`coach_configs/rule.yaml`](coach_configs/rule.yaml) | Pure event-pattern detector — no LLM. Fires when the agent calls the same tool 3× in a row OR when the last 3 tool results were all empty. Injects a canned hint. |
| **`llm_coached`** | [`coach_configs/llm.yaml`](coach_configs/llm.yaml) | LLM-driven detector (asks Claude Haiku "STUCK or OK") + LLM suggester (drafts a specific hint). Per-trial budget cap prevents runaway spend. |

Both coached arms use the *same* coach machinery — different configs
select different `Detector` and `Intervener` classes via a plugin
factory. Any 4×3 combination of detectors × interveners works; the three
shipped configs are just presets.

## The coach architecture

```
CoachConfig (YAML)
   ├── detector: { type, params }   → Rule | LLM | Always | Never
   └── intervener: { type, params } → Hint | LLMSuggest | Kill
   
build_coach(config, llm_call?) → ComposedParticipant + CoachReport

ComposedParticipant runs on a background thread per trial:
   sinks       = [ RollingEventsSink ]    (feeds detector its history)
   controllers = [ CoachController ]      (event-reactive: check → build → submit)
```

The coach is a `ComposedParticipant` from the `intervener` package. It's
task-agnostic — swap the run config's `tasks_glob` to point at a
different task pack and the same coach machinery runs against a
different agent workload.

**Decoupling constraint respected:** the `coach/` package does not
import `tolokaforge.core.llm`. The coach receives its LLM access
through an `LLMCallable` seam (from the intervener package). The
top-level driver (`run_ab_study.py`) is the only place that wraps
`LLMClient` into a concrete callable.

## Running

Prerequisites:

- Docker running (every trial needs the Runner service).
- OpenRouter key in `.env` (or whatever provider the run config uses):
  `OPENROUTER_API_KEY=…`.
- `uv sync` at the repo root.

Run all three arms end-to-end:

```bash
scripts/with_env.sh uv run --package intervener python \
    examples/open_agent_loop_coaching/run_ab_study.py
```

Or one arm at a time:

```bash
scripts/with_env.sh uv run --package intervener python \
    examples/open_agent_loop_coaching/run_ab_study.py --arm solo
```

Then compute the summary:

```bash
uv run python examples/open_agent_loop_coaching/analyze_results.py
```

Expect ~$0.20–$1.00 per arm at the default `repeats=4` (LLM-coached is
the most expensive because of the extra LLM traffic). Whole study is
well under $5 on OpenRouter.

## What you'll see

Terminal output at the end of `analyze_results.py`:

```
════════════════════════════════════════════════════════════════════════════════════════
A/B Coaching Study — arm comparison
════════════════════════════════════════════════════════════════════════════════════════
arm                 trials   pass rate   avg turns     agent $     coach $     total $
----------------------------------------------------------------------------------------
solo                     4        0.25         8.0      0.0803      0.0000      0.0803
rule_coached             4        0.75         5.5      0.0492      0.0000      0.0492  Δpass +0.50
llm_coached              4        0.75         5.3      0.0501      0.0287      0.0788  Δpass +0.50
════════════════════════════════════════════════════════════════════════════════════════

✓ SAVES — rule_coached vs solo (2 trial(s))
  trial=tool_use_public_example_01:1
    solo:    failed (turns=9, cost=$0.0201)
    coached: passed (turns=5, cost=$0.0121)
    triggers (2):
      at_seq=6  same tool 'db_query' called 3× in a row with identical args
      at_seq=14 last 3 tool results all returned empty payloads
  …
```

Numbers above are illustrative. The actual numbers you get from your
own run depend on model non-determinism and network variance.

## The output on disk

```
results/coaching_ab/
├── solo/
│   ├── aggregate.json                     ← tolokaforge writes (pass@k, cost)
│   ├── per_task_metrics.json              ← tolokaforge writes
│   ├── metadata_slices.json               ← tolokaforge writes
│   ├── failure_attribution.json           ← tolokaforge writes
│   └── trials/<task>/<idx>/
│       ├── trajectory.yaml                ← canonical trajectory
│       ├── metrics.yaml                   ← cost + latency + turns
│       ├── grade.yaml                     ← verdict
│       └── … (env, prompts, tool_schemas, logs)
├── rule_coached/
│   ├── … same four aggregate.jsons …
│   └── trials/<task>/<idx>/
│       ├── … same trial artifacts …
│       ├── open_agent_loop.yaml           ← OAL trace (events + interventions)
│       └── coach_report.yaml              ← NEW: per-trial coach bookkeeping
├── llm_coached/
│   └── … same shape …
└── ab_summary.yaml                        ← analyze_results.py writes this
```

`coach_report.yaml` per trial:

```yaml
trial_id: tool_use_public_example_01:1
coach_id: workflow-reviewer-rule
detector_type: rule
intervener_type: hint
interventions_submitted: 2
interventions_by_kind: {inject_message: 2}
ack_outcomes: {accepted: 2}
trigger_events:
  - at_seq: 6
    detector: rule
    reason: same tool 'db_query' called 3× in a row with identical args
    at: "2026-07-17T14:23:11.402Z"
  - at_seq: 14
    detector: rule
    reason: last 3 tool results all returned empty payloads
    at: "2026-07-17T14:23:44.019Z"
coach_llm_calls: 0
coach_input_tokens: 0
coach_output_tokens: 0
coach_cost_usd: 0.0
llm_errors: 0
```

`ab_summary.yaml`:

```yaml
arms:
  solo:
    trials: 4
    pass_rate: 0.25
    avg_turns: 8.0
    agent_cost_usd: 0.0803
    coach_cost_usd: 0.0
    total_cost_usd: 0.0803
    coach_interventions_total: 0
  rule_coached:
    trials: 4
    pass_rate: 0.75
    delta_pass_rate_vs_solo: 0.5
    delta_total_cost_usd_vs_solo: -0.031
    coach_interventions_total: 5
    …
notable_saves:
  - trial_id: tool_use_public_example_01:1
    arm: rule_coached
    solo: failed (turns=9, cost=$0.0201)
    coached: passed (turns=5, cost=$0.0121)
    coach_interventions:
      - {at_seq: 6, reason: "same tool 'db_query' called 3× in a row..."}
notable_harm: []
```

## Interpreting the results

- **`Δpass > 0`** — coaching helped. Compare `total_cost_usd` deltas to
  decide if it's worth it (coaching might reduce agent cost by avoiding
  wasted turns, then rule-coach is *cheaper* than solo).
- **`Δpass ≈ 0`** — coaching didn't help. Either the coach's triggers
  don't correlate with agent failures, or the agent doesn't act on
  hints.
- **`notable_harm` non-empty** — the coach turned some passes into
  failures. This is critical: it means the coach's suggestions actively
  misled the agent. A useful negative finding.
- **`rule_coached` ≈ `llm_coached`** — the simple heuristic captures
  everything the LLM catches. No reason to pay for the LLM coach.
- **`llm_coached` ≫ `rule_coached`** — the LLM's judgement genuinely
  adds value; heuristic isn't enough.

## Configuring your own coach

Both YAML surfaces are meant to be edited. Try:

- **Different detector thresholds:** in `coach_configs/rule.yaml`,
  change `same_tool_repeat_threshold: 3` → `2` (more aggressive) or
  `4` (more conservative).
- **A different intervener with the same detector:** swap
  `intervener.type: "hint"` → `"llm_suggest"` on the rule coach to get
  rule-triggered LLM-drafted hints (cheapest way to add LLM value).
- **Kill-based coaching:** set `role: "admin"` and
  `intervener: { type: kill, params: { after_n: 2 } }`. The coach will
  terminate any trial after 2 stuck detections — useful for compute-cap
  benchmarks.
- **Never coach (control):** `detector: { type: never }` in an open-mode
  arm should reproduce the solo pass rate exactly, up to model
  non-determinism. If it doesn't, something in the open-mode plumbing
  is leaking into the trial — worth investigating.

## Trying a different task

The coach machinery is task-agnostic. Point any of the run configs at a
different task pack:

```yaml
evaluation:
  task_packs: ["examples/native/coding/dataset"]
  tasks_glob: "**/coding_public_example_01/task.yaml"
  output_dir: "results/coaching_ab_coding/rule_coached"
```

The same detectors + interveners work on any tool set. The coach doesn't
know the task — it only sees events on the session bus.

## Layout

```
examples/open_agent_loop_coaching/
├── README.md                       ← this file
├── run_configs/
│   ├── solo.yaml
│   ├── rule_coached.yaml
│   └── llm_coached.yaml
├── coach_configs/
│   ├── rule.yaml
│   └── llm.yaml
├── run_ab_study.py                 ← run the three arms sequentially
├── analyze_results.py              ← compute the A/B summary
└── coach/
    ├── __init__.py
    ├── config.py                   ← Pydantic: CoachConfig + DetectorSpec + IntervenerSpec
    ├── detectors.py                ← Rule + LLM + Always + Never
    ├── interveners.py              ← Hint + LLMSuggest + Kill
    ├── coach_participant.py        ← build_coach() → ComposedParticipant + CoachReport
    └── cost_tracker.py             ← CostTrackingLLMCall + CoachReport
```
