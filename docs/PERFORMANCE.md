# Performance Benchmarks

Tolokaforge ships with a mock load configuration that exercises the full orchestrator/executor pipeline without making external LLM calls. This allows CI and local developers to stress the system safely.

## Environment

| Component | Value |
|-----------|-------|
| CPU | Apple Silicon M-series |
| RAM | 16 GB |
| OS | macOS 15 (Darwin 25.0.0) |
| Python | 3.11.7 |

## Mock Load (8 workers × 100 trials)

To run a mock load test, create a config with `provider: mock`:

```yaml
# Example: mock_load_test.yaml
models:
  agent:
    provider: mock
    name: agent
  user:
    provider: mock
    name: user
orchestrator:
  runtime: docker
  workers: 8
  repeats: 100
  max_turns: 5
evaluation:
  tasks_glob: "tests/data/tasks/minimal_calculation/task.yaml"
  output_dir: "results/mock_load_test"
```

```bash
python -m tolokaforge.cli.main run --config mock_load_test.yaml
```

Configuration highlights:
- Mock `LLMClient` (`provider: mock`) for both agent and user models
- Minimal calculation task under `tests/data/tasks/`
- 8 workers, 100 repeats (100 trials total)
- Orchestrator runtime: docker

### Wall-Clock Results

| Metric | Value |
|--------|-------|
| Real time | **40.62 s** (measured via `/usr/bin/time -l`)
| Throughput | **2.46 trials/s** (~8,860 trials/hour)
| Average turns/trial | 1.0 (mock models exit immediately)
| Average tool calls/trial | 0.0 (mock models skip tools)

### Resource Usage

| Metric | Value |
|--------|-------|
| Peak RSS | 295 MB (`maximum resident set size`)
| Voluntary context switches | 3,922 |
| Involuntary context switches | 443,142 |

### Tokens & Grading

Mock models emit only short acknowledgements. Aggregated metrics therefore show:

- `total_tokens_output`: 300 (≈3 tokens per trial)
- `total_tokens_input`: 0 (no prompts sent to external APIs)
- `avg_latency_s`: `1.06e-4` (model loop returns synchronously)
- `success_rate_micro`: 0.0 (expected—mock agent does not solve task)

These numbers confirm the harness can sustain high throughput even when agent and user LLMs return instantly. Real benchmark runs will be bottlenecked by provider latency instead of orchestrator overhead.

## Historical (Real LLM) Reference

Previous Sonnet 4.5 runs with real API calls (4 workers on a realistic task slice) produced roughly:

| Metric | Value |
|--------|-------|
| Trials/hour | ~400 |
| Avg latency/turn | 35 s |
| Avg tokens/trial | ~36k |
| Estimated cost/trial | ~$0.12 |

These measurements remain valid when you switch the load configuration back to a real provider.

## Notes & Next Steps

1. Commit the generated `output/mock_load_test/aggregate.json` when you want a baseline for regression comparison.
2. Expand the mock scenario to include tool usage if you want to stress the executor under load.
3. When running with real providers, capture per-provider metrics and update this document with cost/latency tables for that environment.

_Last updated: 2025-10-17_
