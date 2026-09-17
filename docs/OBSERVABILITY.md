# Live tracing (ADR-0046)

`observability.tracing` in the run config switches the engine's live trace export on. Every
generation and tool call of a trial leaves the process as an OpenTelemetry span while the trial
runs; the graded trial closes the trace. The exporter is OTLP/HTTP and vendor-neutral: Langfuse
renders the traces like the offline bundle uploader's, any collector receives valid spans.

```yaml
observability:
  tracing:
    exporter: otlp                                  # default: none
    endpoint: https://langfuse.example/api/public/otel/v1/traces   # or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    expect_project: arena                           # the receiver-side project the credentials must open
    run_id: arena/v1/34390073272/1                  # external run identity; default: the engine run id
    run_tag: v1                                     # id namespace
    session_id: arena/v1/gpt6_astra/gpt6_astra/34390073272   # default: run_id
    label: gpt6_astra                               # trace name <label>/<task_id>; default: run dir name
    # the deployment's own tags, <prefix>:<value>; the engine checks the syntax only. The values
    # below are the Toloka arena's vocabulary (team, project, dataset, source, run_kind, scope,
    # config, domain, ci_*); harness:, model*: and task: are set by the exporter itself
    tags: [team:delivery, project:arena, dataset:v1, source:trial, run_kind:eval, scope:full, config:gpt6_astra, domain:ots_19_airlines]
    metadata: {model_stem: gpt6_astra}
    model_name_normalizer: toloka                   # default: none (raw provider/name)
    model_name_rules: tools/benchmark-results-collector/data/model_name_rules.toml
    attach: all                                     # all | core | none: the trial's files as media (below)
```

Install the extra: `pip install 'tolokaforge[otel]'`. The receiver's credentials travel in the
standard `OTEL_EXPORTER_OTLP_HEADERS` environment variable (for Langfuse:
`Authorization=Basic <base64 public:secret>`, plus `X-GitHub-Runner-Key=...` behind the WAF); the
engine never logs them.

## The receiver as environment: a launcher owns the destination

The config may stay vendor-neutral and endpoint-free. When `endpoint` is absent the exporter
reads the standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (as is) or `OTEL_EXPORTER_OTLP_ENDPOINT`
(+ `/v1/traces`); `TOLOKAFORGE_TRACING_TAGS` (comma-separated `<prefix>:<value>`) adds tags to the
config's, and one prefix may never carry two different values; `expect_project` (or
`TOLOKAFORGE_TRACING_EXPECT_PROJECT`) names the receiver-side project the credentials must open.
Before the first export the exporter asks the receiver (Langfuse: `GET /api/public/projects`, the
REST base derived from the endpoint, the OTLP headers as credentials): a mismatch is a
configuration error at run start, with nothing to tear down; a receiver that does not answer
(the external ingest alias returns 403) leaves the run `unverified`; `tracing_receipt.json`
records `expect_project` and `project_verified`.

**One switch.** `LANGFUSE_TRACING_ENABLED=true` turns the exporter on without a tracing block
(or with `exporter: none`). The receiver then comes from the plain Langfuse variables:

| Variable | Meaning |
|---|---|
| `LANGFUSE_BASE_URL` | traces go to `<base>/api/public/otel/v1/traces`, attachments and gradings to `<base>` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | the Basic header (through the `SecretManager` when one is initialised); set both or neither |
| `LANGFUSE_PROJECT` | the project the keys must open (checked before the first export) and the trace's `project:` tag |
| `LANGFUSE_EXTRA_HEADERS` | `k=v,k2=v2`, extra request headers (a gateway's own header) |
| `TOLOKAFORGE_TRACING_RUN_ID`, `_RUN_TAG`, `_SESSION_ID`, `_LABEL` | the run's identity when the config carries none |

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` keep precedence when set.

This is how a deployment keeps its project names out of the engine: the Langfuse connector
(tolokaforge-tools, `langfuse-connector with-destination <name> -- tolokaforge run ...`) resolves
a named destination from its own registry file, checks the keys, and injects the endpoint, the
header, the attachment API base, the `project:<name>` tag and the expected project into the
engine's environment, printing nothing. A config that names its own `endpoint` keeps it.

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

Tags: `harness:tolokaforge`, the model tags (`model:<canonical>`, plus `model_vendor:` and
`model_family:` under the normalizer) and `task:<task_id>` are set by the exporter; `tags:` adds
`<prefix>:<value>` entries and may not use those prefixes. The engine validates only the syntax
(`prefix:value`, lowercase prefix, no whitespace) and the reserved prefixes; which prefixes and
values a deployment allows is the deployment's business (the arena keeps its vocabulary and a
profile file in `tolokaforge-tasks`, and the offline uploader that shares the trace with this
exporter enforces it), so the run-config generator of the private integration writes finished,
validated tags here. Judge generations are not exported live in this version
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

## Attachments: the trial's files on its trace

Once the trial executor has finished writing into the trial directory (the bundle, its
`metrics.yaml` amendment, the service-log capture) it calls the conductor's `trial_persisted`,
and the exporter attaches the regular top-level files of the trial directory to the trace through the receiver's media API
(Langfuse: register, presigned PUT, confirmation; the receiver deduplicates by sha256 per
project, so a `prompts.yaml` shared by every trial of a task is stored once). `env.yaml` and
`trajectory.yaml` travel gzipped (`mtime 0`), the rest as written; hidden files and
subdirectories (video, `services/`) are not attachments. `attach: all` (default) sends every file,
`core` only `task.yaml`, `prompts.yaml`, `tools_schemas.yaml`, `logs.yaml`, `grade.yaml`, `none`
nothing. The trace metadata then carries **manifest v2**, the same document the offline
`langfuse-connector` writes, so `langfuse-connector download` rebuilds the directory byte-exact
from either producer:

```
attachments_schema: 2
attachments: {<file name>: {media_id, media, sha256, stored_sha256, bytes, stored_bytes,
                            content_type, encoding: none | gzip}}
attachments_complete: true | false
attachments_skipped: [{name, rule}]
```

Before a file leaves, its bytes are scanned against the `SecretManager`'s credential values
(keys with secret-like names) and the key-shaped patterns of the connector's data-safety gate
(dotenv secrets, `Authorization` headers, PEM blocks, URL credentials, provider key prefixes,
JWTs, secret-named fields); a hit skips the file, names it in `attachments_skipped` and leaves
`attachments_complete: false`. Bytes are never rewritten. The REST base URL derives from the OTLP
endpoint (`attach_api_base` overrides it), the headers are `OTEL_EXPORTER_OTLP_HEADERS`, each
request has `attach_timeout_s`; the step runs in the trial's thread once the trial is over and
never raises. `tracing_receipt.json` reports `attachments_registered`, `attachments_uploaded`,
`attachments_deduplicated`, `attachments_skipped`, `attachments_failed`, `manifests_sent`,
`manifests_failed`.

## Gradings: the bundle's verdict on the live trace

Once the bundle is on disk the exporter also sends what the live spans could not know
(`observability.tracing.gradings`, default on; needs the same REST base as the attachments): the
run's grading (`grade.yaml`) as a `grading:live:<run_id>` observation under the root with the
judge transcript (`judge_trajectory.yaml`) nested beneath and its scores attached (`binary_pass`,
`score`, `component:*`, `criterion:*`, `trace_check:*`, scope `grading`), the trace-level mirror
of those scores (scope `primary`), the trace's grading keys (`primary_grading`, `gradings`,
`grading_count`, the grade summary) and the simulated user's turns as generations of the user
model. Ids follow the contract the offline connector shares (`ids.py`), and the grading carries
the connector's content fingerprint, so a later connector pass over the same bundle updates
instead of duplicating. The receipt reports `gradings_sent`, `gradings_failed`, `scores_sent`,
`user_generations_sent`. Against an offline upload of the same bundle a live trace then lacks only
the INFO log events and the connector's extra metadata groups.

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
