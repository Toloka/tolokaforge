# Custom Grading Example

This example shows how to define custom grading with deterministic checks and optional LLM judge.

## Included Task

`dataset/tasks/knowledge_reasoning/knowledge_public_example_02/` — a reasoning task
with weighted grading combining state checks, transcript rules, and an LLM judge.

Study its `grading.yaml` for a real-world example of weighted grading.
For the full grading schema reference, see `docs/GRADING.md`.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/custom_grading/dataset/**/task.yaml"
```

## Run

```bash
# Mock provider (no API keys needed)
uv run tolokaforge run --config examples/custom_grading/run_config.yaml

# Real provider (edit run_config.yaml or pass overrides)
scripts/with_env.sh uv run tolokaforge run --config examples/custom_grading/run_config.yaml
```

## Grading Pattern

```yaml
combine:
  method: weighted
  weights:
    state_checks: 0.6
    transcript_rules: 0.2
    llm_judge: 0.2
  pass_threshold: 0.75

state_checks:
  jsonpaths:
    - path_glob: "/env/fs/agent-visible/submissions/*"
      contains_ci: "hypothesis b"

transcript_rules:
  max_turns: 8
  required_actions:
    - action_id: "read_prompt"
      requestor: assistant
      name: read_file
    - action_id: "write_rationale"
      requestor: assistant
      name: write_file

llm_judge:
  model_ref: "mock/mock-judge"
  rubric: |
    Grade the final reasoning quality.
```
