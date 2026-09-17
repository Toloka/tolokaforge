# Live tracing (ADR-0047)

`observability.tracing` in the run config switches the engine's live trace export on. Every
generation and tool call of a trial leaves the process as an OpenTelemetry span while the trial
runs; the graded trial closes the trace. The exporter is OTLP/HTTP and vendor-neutral: Langfuse
renders the traces like the offline bundle uploader's, any collector receives valid spans.

```yaml
observability:
  tracing:
    exporter: otlp                                  # default: none
    endpoint: https://langfuse.example/api/public/otel/v1/traces   # or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    expect_project: pilot                           # the receiver-side project the credentials must open
    run_id: acme/pilot/34390073272/1                # external run identity; default: the engine run id
    run_tag: v1                                     # id namespace
    session_id: acme/pilot/pilot_agent/34390073272  # default: run_id
    label: pilot_agent                              # trace name <label>/<task_id>; default: run dir name
    # the deployment's own tags, <prefix>:<value>; the engine checks the syntax only. The values
    # below are one deployment's vocabulary (team, project, dataset, source, run_kind, scope,
    # config, domain, ci_*); harness:, model*: and task: are set by the exporter itself
    tags: [team:pilot, project:pilot, dataset:v1, source:trial, run_kind:eval, scope:full, config:pilot_agent, domain:pilot-domain]
    metadata: {model_stem: pilot_agent}
    model_name_normalizer: toloka                   # default: none (raw provider/name)
    model_name_rules: deploy/model_name_rules.toml   # the deployment's rules file for the normalizer
    attach: all                                     # all | core | none: the trial's files as media (below)
    projection: full                                # full | gradings | none: what the trial-end pass sends (below)
    profile: deploy/langfuse_tracing.toml           # the deployment profile (below); or TOLOKAFORGE_TRACING_PROFILE
    # environment: development                      # a literal native environment; LANGFUSE_ENVIRONMENT wins
```

Install the observer: `pip install 'tolokaforge[otel]'` (the extra resolves to the `tolokaforge-langfuse`
package, a separate wheel released on its own cadence; "Packaging" below). The receiver's credentials travel in the
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
| `TOLOKAFORGE_TRACING_PROFILE`, `LANGFUSE_ENVIRONMENT`, `TOLOKAFORGE_TRACING_METADATA` | the deployment profile, the environment override and the per-run metadata (the profile section below) |

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
`model_family:` under the normalizer) and `task:<task_id>` are set by the exporter; `tags:`, the
launcher's `TOLOKAFORGE_TRACING_TAGS` and the profile's fixed tags add `<prefix>:<value>` entries
and may not use those prefixes. The engine validates only the syntax (`prefix:value`, lowercase
prefix, no whitespace) and the reserved prefixes; which prefixes and values a deployment allows is
the deployment's business (a deployment keeps its vocabulary and a profile file in its own
repository, and the offline uploader that shares the trace with this exporter enforces it), so
the run-config generator of the private integration writes finished, validated tags here.
Judge generations are not exported live (the judge may run in the runner service); the trial-end
pass adds them from the bundle together with the Langfuse scores.

## Model names as configuration

`model_name_normalizer: none` names the model as the run config spells it (`vendor/model`, or
`provider/name` for a bare name). `toloka` uses `toloka-model-name-normalizer`: one identity for
every route spelling, `model_vendor` / `model_family` tags and facet metadata (generation, tier,
variant, snapshot, ...) plus the rules version and fingerprints, so a trace says which rules named
it. `model_name_rules` layers a deployment's rules file over the library's default; a deployment
keeps its config stems, vendor spellings and abbreviated ids there (`tencent/hy3` is family `hunyuan`).
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

## The trial-end pass: the trace completed from the bundle

Once the bundle is on disk the exporter completes the trace from the files
(`observability.tracing.projection`, `full` by default; needs the same REST base as the
attachments): the **default projection** of a persisted trial, the same records the offline
bundle uploader writes, so a trace traced live never needs a connector pass. The live spans are
the preview, the bundle is the truth: every observation is re-sent from the persisted files under
the shared id contract (an upsert on the receiver), and the trace metadata is sent with every key
of the fixed schema explicit (the receiver merges metadata and an omitted key would persist).

| From the bundle | Records |
|---|---|
| `trajectory.yaml`, `metrics.yaml` | the root observation, one generation per agent turn with its paired usage and cost (by generation id, else positionally), one generation per simulated user turn (the user model, no usage), the trace's input, output, timestamp, status and totals |
| `tool_log.yaml` | one tool span per recorded call, the grader's view (status, executor, latency, sequence, untruncated output) with the transcript's agent-facing text beside it when it differs; the user simulator's own tool calls too |
| `grade.yaml`, `judge_trajectory.yaml`, `judge_inputs.yaml` | the `grading:live:<run_id>` observation with the judge turns beneath, its scores and the trace-level mirror (`gradings: false` leaves the grading out, like the offline `--grades none`) |
| `logs.yaml`, `trajectory.user_reply_guard_events`, `provision_stage`, the run's `LIMIT_HIT.json`, `services/_capture.yaml` | events (WARNING and ERROR always, INFO under `attach: all`) |
| `task.yaml`, `env.yaml`, `engine_run_state.json` | the metadata groups: task facts, the three model configurations with their presets and policies, the environment identity, the redaction stamp, the models fingerprint |
| base64 image blocks in messages | media registered on the observation, the token in its output (raw base64 never enters an ingestion body) |
| the attachment step | manifest v2, complete, in the same trace body |

`projection: gradings` sends only the grading, its scores and the user turns (the behaviour of
the gradings amendment); `none` sends the attachments alone. The pass runs under the attachment
step's budget and breaker, the serialised events go through the same data-safety scan as the
files (a hit sends nothing and counts), and nothing raises into the trial. The receipt reports
`projections_sent`, `projections_failed`, `observations_sent`, `events_sent`, `scores_sent`,
`gradings_sent`, `user_generations_sent`, `media_uploaded`, `media_failed`.

Parity with the offline uploader is guarded by a golden test that lives in both repositories: a
synthetic bundle (`tolokaforge_langfuse/tests/unit/parity_bundle.py`, byte-identical in the connector)
projected by each side and compared as normalised event lists modulo envelope ids, timestamps,
tag order and the documented **producer keys**, whose values differ by producer by design:
`upload_mode` (`live`), `uploader_version` (this engine's `tolokaforge-<version>`),
`trace_time_source` (`live`), `attach_mode`. The live root span adds three keys no
bundle projection carries: `generations_observed`, `tool_calls_observed`, `error`; a Langfuse
receiver adds two more to a trace that arrived over OTLP, `attributes` and `resourceAttributes`
(the trace-level span's raw attributes and the SDK's resource attributes).

## The trace metadata

A trace's metadata is a fixed, flat schema of 34 keys plus the caller's per-run keys, identical
from the trial-end pass and from the offline uploader (`tolokaforge_langfuse.projection.schema_keys()`):

| Group | Keys |
|---|---|
| identity | `task_id`, `trial_index`, `run_id`, `run_tag`, `attempt`, `label`, `harness`, `id_contract` |
| attachments | `attach_mode`, `attachments_schema`, `attachments`, `attachments_complete`, `attachments_skipped` |
| outcome | `status`, `termination_reason`, `grading_error` |
| verdict | `primary_grading`, `gradings`, `grading_count`, `pass`, `score`, `judge_status` |
| usage | `cost_usd`, `tokens_input`, `tokens_output`, `turns`, `tool_calls`, `latency_total_s` |
| models | `model_name`, `user_model`, `judge_model` |
| bookkeeping | `upload_mode`, `uploader_version`, `trace_time_source` |

Everything else a bundle says lives where a reader looks for it, not in the trace's metadata: the
task facts, the model configurations, the environment identity and the redaction stamp in the
attached `task.yaml`, `env.yaml` and `metrics.yaml`; the per-generation usage on the generations;
the criteria and trace-check detail on the grading observation and its scores; the model facets
and the launcher's tags as tags; the rules and profile versions in the native `version` field.
The bound is the receiver's: Langfuse's trace page renders its metadata table only up to 100
top-level keys and shows nothing above that (3.205.1, verified 2026-09-17), so the schema stays
well under it with room for the caller's keys, and a caller key that names a schema key is a
configuration error.

## The deployment profile

Everything a deployment decides about its traces and the engine must not know as a value arrives
at run time in one TOML file, `observability.tracing.profile` or `TOLOKAFORGE_TRACING_PROFILE`
(`tolokaforge_langfuse/profile.py`; `python -m tolokaforge_langfuse.profile <file>`
validates one). Neutral example:

```toml
schema = 1
version = "acme-2026.09.17.1"              # joins the native `version` field

[environment]                              # the receiver's native environment
from_tag = "run_kind"                      # or: literal = "development"
default = "development"
[environment.values]
eval = "production"

[tags]
fixed = ["team:pilot"]                     # tags every trace of the deployment carries

[metadata.fixed]                           # metadata every trace carries
deployment = "pilot"

[models]
rules = "model_name_rules.toml"            # selects the toloka normalizer with these rules (relative to this file)
```

The engine validates the shape and applies it mechanically. A profile that does not load, an
environment outside the receiver's alphabet (lowercase letters, digits, `-`, `_`, at most 40
characters, never starting with `langfuse`), a fixed tag under a prefix the exporter sets itself,
two values under one prefix, or a metadata key the projection writes itself is a configuration
error at run start.

**Per-run values from the launcher.** `LANGFUSE_ENVIRONMENT` (a literal) overrides the profile's
rule and the config's `environment`; `TOLOKAFORGE_TRACING_METADATA` (`key=value,...`) carries the
per-run metadata the offline command receives as `--metadata` (profile fixed keys < config
`metadata` < the variable; a key of the fixed schema is refused). The Langfuse connector's
`with-destination` speaks this dialect (`--tracing-profile`, `--metadata`, the registry entry's
`environment`), so one launcher serves a local run and the CI.

**Native fields.** `environment` rides on every span (the receiver fixes a trace's environment at
the first write it sees, verified on Langfuse 3.205.1 on 2026-09-17: a trace written without it
reads `default` and no later update repairs it) and on every ingestion body of the trial-end pass
(observations and scores are filed under `default` otherwise, whatever the trace says);
`release` is this engine's own version (`tolokaforge-<version>`, also written into
`run_identity.json` as `engine_version` for the offline uploader); `version` is the producer's
identity plus the model-name rules and the profile it ran under
(`tolokaforge-<version>+<rules version>+<profile version>`).

## Packaging: the seam in the engine, the observer in its own wheel

The engine owns the seam and nothing receiver-shaped: `tolokaforge/observability/observer.py`
(the `TrialObserver` hooks, the null and composite observers, the receipt), `ids.py` (the id
contract shared with the offline uploader) and `factory.py` (the run identity, `run_identity.json`,
`tracing_receipt.json`, and the discovery of the installed **trial-observer plugins**). Everything
Langfuse-shaped is the `tolokaforge-langfuse` distribution (`tolokaforge_langfuse/` in this
repository, a workspace member released on its own `langfuse-vX.Y.Z` cadence, `docs/RELEASING.md`):
the OTLP observer, the trial-end projection, the gradings pass, the media and ingestion calls, the
attachment manifest, the deployment profile and the model-name resolution.

A plugin is a callable registered under the `tolokaforge.trial_observers` entry-point group with the
signature `build(tracing, identity, *, engine_run_id, output_dir) -> TrialObserver | None`. At run
start the engine resolves the identity, asks every installed plugin in name order and composes the
answers; `None` means "nothing asks for me in this run" (the Langfuse plugin answers `None` unless
`exporter: otlp` or `LANGFUSE_TRACING_ENABLED` asks). `exporter: otlp` with no plugin answering,
or a plugin that cannot be imported, is a configuration error at run start. The pairing is checked
by the plugin: the engine's `PLUGIN_API_VERSION` (the `build` signature, the observer hooks and the
id contract) must equal the plugin's `__api_version__`, and a mismatch names both versions. So a
fix to the projection, the profile or the attachment step reaches a deployment by moving the
`tolokaforge-langfuse` pin while the engine pin stays; a change to the hooks or the ids moves both,
engine first. `observability.tracing.options` carries plugin settings the engine has no field for.

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
