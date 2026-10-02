# 0052. The agent reply contract — how a solo agent is told to answer

- **Status:** Accepted
- **Date:** 2026-09-29
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Relates to:** [ADR-0011](0011-seam-and-declaration-conventions.md) § "Do not
  introduce a Protocol when…", [ADR-0028](0028-multi-actor-turn-policy.md),
  [ADR-0032](0032-agent-completion-is-structural.md)

## Context and Problem Statement

A task's system prompt has always answered one question: *what is the work?*
On a benchmark where the agent works alone in a container and the grader reads
the container rather than the transcript, a second question turns out to matter
as much: *how should the agent answer each turn?*

Measured on the same ten tasks, the same single `bash` tool and the same
provider account, `moonshotai/kimi-k2.7-code`:

| | this engine | an external harness |
|---|---|---|
| assistant messages carrying any text | 5 of 2,239 (0.2%) | 2,333 of 2,333 (100%) |
| turns used | 79.7 (against a 90 cap) | 46.7 |
| trials that never edited a file | 23 of 50 | 2 of 50 |
| score | 0.321 | 0.826 |

The external harness scores 2.6× higher in *fewer* turns. It is not the toolkit
— it offers one shell tool, as we do. It is not the turn budget — raising ours
from 50 to 90 moved 0.286 to 0.321 at double the cost. It is not model
capability — the same model narrates on every turn when asked to. The engine's
own prompt for such a task is 34 words and asks for nothing, and a grader that
never reads the transcript gives a model no reason to keep one. A second model
shows the same signature: `google/gemini-3.1-pro-preview`, 1.05% prose, 19 of
25 trials exhausting their turns.

So the engine needs a way to state a reply contract — write down what you
observed, predict what comes next, say when you are done — as a first-class,
selectable input, not a string buried in one adapter.

## Decision Drivers

- **A contract is not a task.** It is true for a whole class of tasks and a
  whole model, and false for a conversation with a user. It must be selectable
  independently of the task's own document and must compose with it, not
  replace it.
- **Completion stays structural.** `tests/canonical/test_agent_prompt_exit_token.py`
  forbids a sentinel token in any agent prompt: completion is decided by the
  turn policy (ADR-0032). The contract may describe that rule; it may not
  invent one.
- **Length is a cost.** The system prompt is re-sent every turn, and the
  engine's cost advantage over the external harness comes from sending less.
- **A graded input must be visible.** Changing what the model is told changes
  what the number means, so the text is data under a generation counter with a
  test that pins it, not an incidental string.

## Considered Options

1. **A string in the `terminal_bench` adapter.** Cheapest, and wrong: the
   problem is a model's behaviour when it works alone, which is not specific to
   one benchmark, and it would be unreachable from a task or a preset.
2. **A new `prompt_policy` value.** Rejected. That slot is a provider-defect
   compensator — all three shipped values append schema hints — and its
   `enrich(system, tools)` signature cannot see the task, the interaction mode
   or the turn budget. It would fire on conversational customer-service tasks
   and on the judge path alike.
3. **A new Protocol and entry-point group.** Rejected on ADR-0011's own terms:
   a Protocol is not warranted for a pure data transformation from typed input
   to typed output with no side effects, which is what rendering a named string
   is. A second *rendering mechanism* cannot be named; only second *contents*,
   which are data.
4. **Data plus a pure resolver, selected from a task and from a preset.**
   Chosen — ADR-0011 Pattern B.

## Decision

### The contract library

`tolokaforge/core/agent_prompt_contract.py` holds `CONTRACTS`, a name → text
map, a `GENERATION` counter bumped whenever shipped text changes, and
`resolve_agent_prompt_contract(selector, *, task_dir)`. A bare name is looked up
in `CONTRACTS`; anything else is read as a path relative to *task_dir*, the same
rule `TaskConfig.system_prompt` follows, so a pack may ship its own. An
unmatched selector raises rather than being ignored — a silently dropped
selector runs a whole task set on the wrong prompt.

One contract ships: `reasoning_agent`.

### Composition, not replacement

`build_system_prompt` puts the contract first and the task's own document after
it. The contract states the standing rule for every turn; the body states this
particular job, and a model reading top-down meets the rule before the
specifics. The generic `"You are a helpful assistant."` persona is dropped when
a contract supplies one of its own, because two personas in one prompt
contradict each other.

### Two selectors, one override order

1. `task.policies["agent_system_prompt"]` — an inline prompt reproduced byte for
   byte. Unchanged, and still wins outright.
2. `TaskConfig.agent_prompt_contract` — this task names a contract.
3. `ModelCapabilities.default_agent_prompt_contract` — this model's preset names
   one, applied only when `interaction_mode` is `agent_only`.

The preset default is modelled on `default_max_turns`: a preset-level value the
task can override, resolved to text by the same function. The
interaction-mode gate is not a convenience — the shipped text tells an agent
that a message carrying no tool call ends the task, which is exactly what
`AgentOnlyTurnPolicy` does and exactly what a conversational turn policy does
not. A task may still name a contract in either mode; a blanket preset default
may not.

### The adapter stops speaking for the engine

The `terminal_bench` adapter wrote `policies["agent_system_prompt"]` on every
task, which is the branch that wins outright — so no engine-level default could
ever reach a Terminal-Bench trial. It now writes that key only when a prompt
file was supplied for byte-exact replay, and otherwise leaves it absent.

### Ported demand, not ported format

The external harness wraps replies in a JSON envelope the model must emit
correctly, and pays for it: its archived corpus carries steps whose only
observation is a parse error, plus a repair loop. That envelope exists because
the harness parses free text. This engine calls tools natively, so the contract
asks for reasoning in the message body *beside* a real tool call — no envelope,
no parse failures, and the model keeps the structured tool interface its
provider tuned it for.

Two things the contract deliberately omits. It does not ask the model to batch
commands: the external harness averages 2.35 shell commands a turn against our
1.24, which is most of why it needs fewer turns, but its prompt never asks for
it — the batching falls out of a schema taking an array of commands. That is a
tool-shape change, and asking for it in prose would be cargo cult. And it does
not echo the engine's customer-service instruction, which tells an agent it may
send a message **or** call a tool and "cannot do both at the same time" — right
when a user is waiting for the floor, and exactly backwards for an agent working
alone.

## Consequences

### Positive

- A model that does not narrate unprompted can be given a reason to, without
  editing any task and without a framework change per benchmark.
- The text is reviewable as data: a diff to a shipped contract shows up as a
  `GENERATION` bump and a failing canonical test.
- A pack may ship its own contract beside its tasks with no code.
- `agent_system_prompt` keeps its byte-exact replay guarantee.

### Negative / Trade-offs

- **It changes a graded input.** A score produced with a contract is not
  comparable to one produced without it, and runs that mix the two are not
  comparable to either. This is the reason the field is visible in
  `task_config.json` and pinned by a canonical test.
- **It costs tokens on every turn.** The shipped contract is ~1.15 KB, re-sent
  each turn.
- **It measures the model plus our coaching.** That is a deliberate choice about
  what the benchmark reports, and it should be stated wherever the numbers are
  published rather than absorbed silently.

### Follow-ups

- Which presets opt in, on the evidence of a per-model control run.
- Switching the `terminal_bench` adapter to `interaction_mode: agent_only`,
  which is what makes the preset default reachable there.

## Links

- [ADR-0011](0011-seam-and-declaration-conventions.md) — Pattern B, and why no
  Protocol here.
- [ADR-0028](0028-multi-actor-turn-policy.md) — `interaction_mode` and the turn
  policy registry.
- [ADR-0032](0032-agent-completion-is-structural.md) — completion is structural.
- `docs/TASKS.md` § Agent reply contract, `docs/LLM_LAYER.md` § Preset-level
  reply contract.
