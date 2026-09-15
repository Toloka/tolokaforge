# Live tracing (ADR-0046)

`observability.tracing` in the run config switches the engine's live trace export on. Every
generation and tool call of a trial leaves the process as an OpenTelemetry span while the trial
runs; the graded trial closes the trace. The exporter is OTLP/HTTP and vendor-neutral: Langfuse
renders the traces like the offline bundle uploader's, any collector receives valid spans.

```yaml
observability:
  tracing:
    exporter: otlp                                  # default: none
    endpoint: https://langfuse.example/api/public/otel/v1/traces
    run_id: toloka-arena/v1/34390073272/1           # external run identity; default: the engine run id
    run_tag: v1                                     # id namespace (contract v1)
    session_id: toloka-arena/v1/gpt6_astra/gpt6_astra/34390073272   # default: run_id
    label: gpt6_astra                               # trace name <label>/<task_id>; default: run dir name
    tags: [source:arena-trial, benchmark:toloka-arena, arena_version:v1, config:gpt6_astra, domain:ots_19_airlines]
    metadata: {model_stem: gpt6_astra}
    model_name_normalizer: toloka                   # default: none (raw provider/name)
    model_name_rules: tools/benchmark-results-collector/data/model_name_rules.toml
```

Install the extra: `pip install 'tolokaforge[otel]'`. The receiver's credentials travel in the
standard `OTEL_EXPORTER_OTLP_HEADERS` environment variable (for Langfuse:
`Authorization=Basic <base64 public:secret>`, plus `X-GitHub-Runner-Key=...` behind the WAF); the
engine never logs them.

## What a trace looks like

| Engine event | Span | Ids (contract v1, shared with the uploader) |
|---|---|---|
| trial (opened by the conductor, closed after grading) | root span `trial <task>/<trial>`, trace name `<label>/<task_id>`, session, tags, `langfuse.trace.metadata.*` (task, trial, attempt, run id, status, termination, pass, score, tokens, cost, model facets), input = first user message, output = last assistant message | `trace_id = uuid5(NS, "trace\|<run_tag>\|<run_id>\|<task_id>\|<trial_index>\|<attempt>")`, root span `uuid5(NS, "obs\|<trace>\|root\|0")[:16]` |
| assistant turn (after the message is recorded) | generation `assistant turn <i>`, model name, usage details (`input` = prompt minus cache reads, `output`, `total`), cost, last 6 request messages as input, text + tool calls as output | `obs\|<trace>\|gen\|<i>` |
| tool result (after the message is recorded) | span `tool: <name>`, redacted arguments as input, output or error, `ERROR` level on failure | `obs\|<trace>\|tool\|<i>` |

`<i>` is the message's position in `trajectory.messages`, so a later upload of the bundle by the
`langfuse-uploader` (tolokaforge-tools) lands on the same observations. For that parity the
bundle records `trajectory.attempt_id`, and a run with tracing on writes `run_identity.json`
(`run_id`, `run_tag`) into the run directory; the uploader reads both.

Tags: `harness:tolokaforge` and the model tags (`model:<canonical>`, plus `model_vendor:` and
`model_family:` under the normalizer) are set by the exporter; `tags:` adds `<prefix>:<value>`
entries and may not use those prefixes. Judge generations are not exported live in this version
(the judge may run in the runner service); the uploader's `attach-grades` adds them and the
Langfuse scores after the run.

## Model names as configuration

`model_name_normalizer: none` names the model as the run config spells it (`vendor/model`, or
`provider/name` for a bare name). `toloka` uses `toloka-model-name-normalizer`: one identity for
every route spelling, `model_vendor` / `model_family` tags and facet metadata (generation, tier,
variant, snapshot, ...) plus the rules version and fingerprints, so a trace says which rules named
it. `model_name_rules` layers a deployment's rules file over the library's default; the arena keeps
its config stems, vendor spellings and abbreviated ids there (`tencent/hy3` is family `hunyuan`).
Selecting the normalizer without the package installed, or a rules file that does not load, is a
configuration error at run start, never a silent fallback.

## Delivery

Spans go through a bounded queue exported by a background thread (`queue_size`,
`export_batch_size`, `export_interval_s`); a full queue drops spans and counts them, the agent
loop never waits. At run end the queue is flushed within `flush_timeout_s` and
`tracing_receipt.json` in the run directory reports `spans_queued`, `spans_exported`,
`spans_dropped`, `export_failures`, `flushed`; the same counts go to the log (a warning when
anything was dropped). If the receiver is unreachable the flush gives up after `flush_timeout_s`
and counts the rest as dropped, so a run never waits on its traces. Tool-call **arguments** (a
mapping) pass through the engine's `SensitiveKeyRedaction`; tool outputs and message text are free
text, which key-based redaction cannot cover, so they are capped at `attribute_max_chars` but not
redacted; base64 image blocks never leave through spans. The receiver's headers are read through
the `SecretManager` (`OTEL_EXPORTER_OTLP_HEADERS`) so their value is redacted from the engine's logs.
