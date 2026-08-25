# `grader_rpc` end-to-end reference

Hands one trial's completed transcript to the standalone
`tolokasoft1/tolokaforge-grader` container over
`GraderService.Grade`. The trial's every grading component
(`state_checks`, `transcript_rules`, `trace_checks`, `llm_judge`,
`custom_checks`) runs inside the grader container against a
`LiveRunnerCallbackGradingSubstrate` dialled at the runner's
SubstrateService — read-only, no runner mutation.

The [`run_config.yaml`](run_config.yaml) here pins the intended shape:
`grader.name: grader_rpc` and `grader.expose_substrate: false` (the
compose file's `RUNNER_EXPOSE_SUBSTRATE=true` env on the runner opens
the substrate surface — the orchestrator does not need to re-forward
the flag).

## Prerequisites

- Docker with the compose plugin.
- A provider key in [`../../.env`](../../.env.example) for any task that
  declares `llm_judge` grading. Keyless grading (state_checks,
  transcript_rules, trace_checks, custom_checks) needs no key.

## Bring the stack up

From this directory's grandparent
([`deploy/standalone/`](../../docker-compose.yaml)):

```bash
docker compose up -d --wait
```

`--wait` blocks on every service's HEALTHCHECK, including the grader's
gRPC channel-ready probe. Once it returns, `docker compose ps` shows all
five services `healthy`.

## Verify the grader dispatches a Grade

The most direct verification hits `localhost:50052` from the host:

```python
from tolokaforge.core.models import Grade, GradeComponents
from tolokaforge.grader.client import GrpcGraderClient
from tolokaforge.runner.models import RunnerGradingConfig, TranscriptRulesConfig
from tolokaforge.runner.models import TaskDescription

grading = RunnerGradingConfig(
    weights={"transcript_rules": 1.0},
    transcript_rules=TranscriptRulesConfig(must_contain=["done"]),
    pass_threshold=0.5,
)
task = TaskDescription(
    task_id="demo",
    name="demo",
    category="demo",
    description="demo",
    adapter_type="native",
    system_prompt="you are a helper",
    grading=grading,
)

with GrpcGraderClient(grader_address="localhost:50052") as client:
    result = client.grade(
        trial_id="demo:0",
        llm_messages_json='[{"role":"assistant","content":"done"}]',
        termination_reason="agent_done",
        task_config_json=grading.model_dump_json(),
        task_description_json=task.model_dump_json(),
        runner_substrate_address="runner:50051",
        agent_system_prompt="you are a helper",
    )
print(result["success"], result["grade"]["score"])
```

The grader logs a `Grader service produced a Grade` line with the trial
id, score, and binary-pass verdict — that entry comes from the grader
container's own logger.

`docker compose logs grader` prints the same lines with grader-side
context (`trial_id`, `score`, `binary_pass`).

## Tear down

```bash
docker compose down -v
```

The `-v` drops the `rag_data` named volume so the next bring-up starts
from a clean corpus.

## Wiring the orchestrator to grader:50052

`RunConfig.grader.name: grader_rpc` selects the grader-service transport
at run time. Today the orchestrator threads the runner's `runner_address`
onto every grader context and the `grader_rpc` factory falls back to it
when no dedicated grader address is set — so an orchestrator run against
this compose without extra plumbing would dial `runner:50051` and miss
the grader container entirely. Wiring the operator-facing
`grader_address` (env var or config field) is tracked as a follow-up on
issue #1263; the `GrpcGraderClient` snippet above is the load-bearing
end-to-end verification until then.
