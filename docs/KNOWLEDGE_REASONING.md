# Knowledge/Reasoning Benchmark Type

## Goal

Support both:
1. single-turn reasoning/QA benchmarks
2. multi-turn reasoning chains with optional tools

under one harness format and reporting pipeline.

## Modes

### Single-turn mode

1. Task provides one prompt/problem.
2. Agent returns one final answer.
3. Scoring uses exact/regex/structured checks and/or rubric.

### Multi-turn mode

1. Task uses orchestrator loop with user simulator.
2. Agent may call tools when enabled.
3. Scoring includes correctness plus process/tool-use expectations.

## Why this stays in Tolokaforge

1. Shared task/scorer/result format across interactive and non-interactive benchmarks.
2. Ability to mix reasoning tasks with tool-use or long-horizon constraints.
3. Unified CI and reporting for all benchmark types.

## Task Authoring Notes

1. Set `category: "knowledge_reasoning"`.
2. For single-turn tasks, keep tool list empty unless required.
3. For multi-turn tasks, define user simulator behavior and termination clearly.
4. Add failure-mode checks (invalid format, unsupported claim, unsafe inference pattern).

## Example Locations

1. `tasks/knowledge_reasoning/knowledge_public_example_01/`
2. `tasks/knowledge_reasoning/knowledge_public_example_02/`
   - Includes a multi-turn scripted follow-up step before final stop.
