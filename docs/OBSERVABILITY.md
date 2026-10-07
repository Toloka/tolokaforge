# Live tracing (ADR-0047)

`observability.tracing` in the run config selects an installed trial-observer plugin. The
Langfuse plugin described here exports each generation and tool call as an OpenTelemetry span
while the trial runs; the graded trial closes the trace. On a Langfuse v4 receiver it writes each
trial once, when it is persisted, and sends live rows only as opt-in previews (§ The write-once
producer layout). Its OTLP/HTTP exporter sends valid spans
to any collector, and its receiver-specific REST operations complete the trace in Langfuse.
The engine's observer seam also accepts other backends.

```yaml
observability:
  tracing:
    exporter: otlp                         # default: none
    endpoint: https://langfuse.example/api/public/otel/v1/traces
    run_id: acme/pilot/34390073272/1         # default: engine run id
    run_tag: v1                            # id namespace
    session_id: acme/pilot/pilot_agent/34390073272  # default: run_id
    label: pilot_agent                     # the run's label; trace name <label>/<task_id> unless the profile's [trace] names it
    tags: [team:pilot, dataset:v1, run_kind:eval, scope:full, config:pilot_agent]
    metadata: {model_stem: pilot_agent}
    options:
      langfuse:
        expect_project: pilot              # project the credentials must open
        model_name_normalizer: toloka      # default: none (raw provider/name)
        model_name_rules: deploy/model_name_rules.toml
        attach: all                        # all | core | none: trial files as media
        gradings: true                     # include grading transcript and scores
        projection: full                   # full | gradings | none: trial-end records
        profile: deploy/langfuse_tracing.toml  # a path, the profile inline, or TOLOKAFORGE_TRACING_PROFILE
        # project: pilot                   # the deployment block: "The deployment profile" below
        # environments: {test: {accepts: [trial]}}
        # environment: development         # LANGFUSE_ENVIRONMENT wins
        # attach_api_base: https://langfuse.example  # default: derived from endpoint
        # attach_timeout_s: 60              # per-request timeout
        # attach_budget_s: 120              # whole-trial attachment budget
        # retry: {statuses: [403, 429, 503], flush_grace_s: 240}  # refusals posted again (§ Retries)
```

The engine's `TracingConfig` owns only exporter selection, endpoint, run identity, session/label,
service name, tags, metadata, queue/flush limits and span content limits. `exporter` may name any
installed plugin's supported exporter. `options` is an opaque mapping keyed by plugin name;
the engine passes it through unchanged. The Langfuse plugin validates `options.langfuse` against
its strict `LangfuseConfig` before contacting the receiver. All receiver-specific settings below
(`expect_project`, `project`, `project_id`, `environments`, `attach`, `gradings`, `projection`,
`attach_*`, `retry`, `profile`, `environment`, `model_name_*`) live in that namespace. Defaults and environment precedence are unchanged.

A receiver setting left at the tracing block's top level, and an unknown key inside the Langfuse
namespace, are both errors at config load; they are never silently ignored. Adding a
Langfuse-only option needs no engine release.


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
records `expect_project` and `project_verified` in its `details` entry with `exporter: langfuse`.

**One switch.** `LANGFUSE_TRACING_ENABLED=true` turns the exporter on without a tracing block
(or with `exporter: none`). The receiver then comes from the plain Langfuse variables:

| Variable | Meaning |
|---|---|
| `LANGFUSE_BASE_URL` | traces go to `<base>/api/public/otel/v1/traces`, attachments and gradings to `<base>` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | the Basic header (through the `SecretManager`); set both or neither |
| `LANGFUSE_PROJECT` | the project the keys must open (checked before the first export) and the trace's `project:` tag (§ The project and its environments) |
| `LANGFUSE_EXTRA_HEADERS` | `k=v,k2=v2`, extra request headers (a gateway's own header) |
| `TOLOKAFORGE_TRACING_RUN_ID`, `_RUN_TAG`, `_SESSION_ID`, `_LABEL` | the run's identity when the config carries none |
| `TOLOKAFORGE_TRACING_PROFILE`, `LANGFUSE_ENVIRONMENT`, `TOLOKAFORGE_TRACING_METADATA` | a profile file when the config names none, the environment (the selector when the config declares `environments`) and the per-run metadata (the profile section below) |
| `LANGFUSE_TRACING_PREVIEWS` | `true` / `1` / `yes` / `on`: a v4 receiver also gets the live rows as declared previews while a trial runs; off by default (§ The write-once producer layout) |

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` keep precedence when set.

This is how a deployment keeps its credentials out of its configuration: the Langfuse connector
(tolokaforge-tools, `langfuse-connector with-environment <name> --config project.yaml --
tolokaforge run ...`) reads the deployment's block, checks the keys open its project, and injects
the endpoint, the header, the attachment API base, the environment and the expected project into
the engine's environment, printing nothing. A config that names its own `endpoint` keeps it.

## What a trace looks like

| Engine event | Span | Ids (contract v1, shared with the uploader) |
|---|---|---|
| trial (opened by the conductor, closed after grading) | root observation `trial` (an `agent` observation), the trace's name and user (§ The trace's name and user), session, tags, `langfuse.trace.metadata.*` (task, trial, attempt, run id, status, termination, pass, score, tokens, cost, model facets), input = first user message, output = last assistant message | `trace_id = uuid5(NS, "trace\|<run_tag>\|<run_id>\|<task_id>\|<trial_index>\|<attempt>")`, root span `uuid5(NS, "obs\|<trace>\|root\|0")[:16]` |
| assistant turn (after the message is recorded) | generation `agent` (a judge's: `judge`), its position as `message_index` metadata, model name, usage details (`input` = the prompt without its cache reads and cache writes, `output`, `cache_read_input_tokens` and `cache_creation_input_tokens` when the call states them, `total` = prompt + completion, so the components add up to it, except when the cache counters exceed the prompt: `input` then floors at 0, and the projection's `usage_clamped` says so), cost (§ Cost on a trace), last 6 request messages as input, text + tool calls as output | `obs\|<trace>\|gen\|<i>` |
| tool result (after the message is recorded) | `tool` observation `tool: <name>` (a judge's: `judge tool: <name>`), redacted arguments as input, output or error, `ERROR` level on failure | `obs\|<trace>\|tool\|<i>` |

**Names and kinds.** An observation's name says what it is, never where: `trial`, `agent`, `user
simulator`, `judge`, `tool: <name>`, `grading`, and the position rides in its metadata
(`message_index`, `call_index`), so the receiver's views by observation name group one kind of
observation. Every observation is typed: the root is an `agent`, a tool execution a `tool`, a
grading an `evaluator`, a model call a `generation`, a log line or a guard an `event`; each travels
as its own ingestion event type (`agent-create`, `tool-create`, `evaluator-create`) or OTLP
`langfuse.observation.type`.

**Clocks.** A message's `ts` is when it was recorded, so a generation runs from the message before
it to its own (the model call that produced it) and a tool without a log record from the message
before its result to the result; the first message's work starts with the trial. A missing clock
collapses a window onto the clock there is, never stretches it. The live spans carry the loop's
own call timing.

**The trace's name and user** are the deployment's (the profile's `[trace]`, § The deployment
profile): `name` is a template over the trace's tag values and the run's label (`{dataset}/{domain}`
names a trial after what it is a case of), `user` is `none` or `model`, the agent's model identity
under the deployment's model-name rules, so the receiver's views by user are views by model.
Without a profile a trace is named `<label>/<task_id>` and has no user. Both producers and every
row (previews and error roots included) follow the same rule, which is why a template may name only
what a trial's rows carry from its start: the caller's and the launcher's tags, the task and the
model identity with its facets, never the tags only the bundle gives (`reasoning_*`, `route`: the
profile refuses them). A model the deployment's rules cannot read gives a trace no user, on the live
rows as in the bundle pass.

The same `trace_id` names the trial's conversation to a model's session header
(`<trace_id>-agent` / `<trace_id>-user`, [LLM_LAYER.md § Session header](LLM_LAYER.md#session-header)),
so a gateway that logs that header can join its request log to the trace.

`<i>` is the message's position in `trajectory.messages`, so a later upload of the bundle by the
`langfuse-uploader` (tolokaforge-tools) lands on the same observations. For that parity the
bundle records `trajectory.attempt_id`, and a run with tracing on writes `run_identity.json`
(`run_id`, `run_tag`) into the run directory; the uploader reads both.

Tags: `harness:tolokaforge`, the model tags (`model:<canonical>`, plus `model_vendor:` and
`model_family:` under the normalizer) and `task:<task_id>` are set by the exporter; `tags:`, the
launcher's `TOLOKAFORGE_TRACING_TAGS` and the profile's fixed tags add `<prefix>:<value>` entries
and may not use those prefixes. The Langfuse plugin validates the syntax (`prefix:value`, lowercase
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

Before a file leaves, its bytes are scanned with the run's data-safety gate (§ Delivery): the
credential values the process holds and the key-shaped patterns of the connector's data-safety
gate (dotenv secrets, `Authorization` headers, PEM blocks, URL credentials, provider key prefixes,
JWTs, secret-named fields); a hit skips the file, names it in `attachments_skipped` and leaves
`attachments_complete: false`. Bytes are never rewritten. The REST base URL derives from the OTLP
endpoint (`attach_api_base` overrides it), the headers are `OTEL_EXPORTER_OTLP_HEADERS`, each
request has `attach_timeout_s`; the step runs in the trial's thread once the trial is over and
never raises. A request the receiver refused without reading it goes again within the trial's
budget (§ Retries). Under `extra`, `tracing_receipt.json` reports the `langfuse.`-prefixed counters
`attachments_registered`, `attachments_uploaded`,
`attachments_deduplicated`, `attachments_skipped`, `attachments_failed`, `manifests_sent`,
`manifests_failed`.

## The trial-end pass: the trace completed from the bundle

Once the bundle is on disk the exporter completes the trace from the files
(`observability.tracing.options.langfuse.projection`, `full` by default; needs the same REST base as the
attachments): the **default projection** of a persisted trial, the same records the offline
bundle uploader writes, so a trace traced live never needs a connector pass. The live spans are
the preview, the bundle is the truth: every observation is re-sent from the persisted files under
the shared id contract (an upsert on the receiver), and the trace metadata is sent with every key
of the fixed schema explicit (the receiver merges metadata and an omitted key would persist).

| From the bundle | Records |
|---|---|
| `trajectory.yaml`, `metrics.yaml` | the root observation, one generation per agent turn and one per simulated user turn (the user model), each with the usage and cost of the call it pairs with (§ Cost on a trace), one generation for every recorded call no message pairs with, the trace's input, output, timestamp, status and totals. Under `actors.user.tool_turns: isolated` each user tool step is a user generation whose output carries its calls, its results are `user_tool` spans even without a `tool_log.yaml`, and the trace's input is the first user turn of the dialogue, not a step taken before it. The agent's opening line, declared in `task.yaml`'s `user_actor.first_agent_message`, is an `agent opening line` event, not a generation, and takes no usage; the pinned opener after it is still no user generation |
| `tool_log.yaml` | one tool span per recorded call, the grader's view (status, executor, latency, sequence, untruncated output) with the transcript's agent-facing text beside it when it differs; the user simulator's own tool calls too |
| `grade.yaml`, `judge_trajectory.yaml`, `judge_inputs.yaml` | the `grading` observation (an evaluator, `grading_id: live:<run_id>` in its metadata) with the judge turns beneath, its scores and the trace-level mirror (`gradings: false` leaves the grading out, like the offline `--grades none`) |
| `logs.yaml`, `trajectory.user_reply_guard_events`, `provision_stage`, the run's `LIMIT_HIT.json`, `services/_capture.yaml` | events (WARNING and ERROR always, INFO under `attach: all`) |
| `task.yaml`, `env.yaml`, `engine_run_state.json` | the metadata groups: task facts, the three model configurations with their presets and policies, the environment identity, the redaction stamp, the models fingerprint |
| base64 image blocks in messages | media registered on the observation, the token in its output (raw base64 never enters an ingestion body) |
| the attachment step | manifest v2, complete, in the same trace body |

`projection: gradings` sends only the grading, its scores and the user turns; `none` sends the attachments alone. The pass runs under the attachment
step's budget and breaker, the events go through the same data-safety scan as the live spans
(§ Delivery: a hit sends nothing and counts), and nothing raises into the trial. The receipt
counts the pass under `extra` (§ Delivery lists every counter): `projections_sent`,
`projections_failed`, `projections_refused_secret`, `observations_sent`, `events_sent`,
`scores_sent`, `gradings_sent`, `gradings_failed`, `gradings_refused_secret`,
`user_generations_sent`, `media_uploaded`, `media_failed`.

Parity with the offline uploader is guarded by a golden test that lives in both repositories: a
synthetic bundle (`tolokaforge_langfuse/tests/unit/parity_bundle.py`, byte-identical in the connector)
projected by each side and compared as normalised event lists modulo envelope ids, timestamps,
tag order and the documented **producer keys**, whose values differ by producer by design:
`upload_mode` (`live`), `uploader_version` (this engine's `tolokaforge-<version>`),
`trace_time_source` (`live`), `attach_mode`. The live root span adds three keys no
bundle projection carries: `generations_observed`, `tool_calls_observed`, `error`; a Langfuse
receiver adds three more to a trace that arrived over OTLP, `attributes`, `resourceAttributes` and `scope`
(the trace-level span's raw attributes, the SDK's resource attributes and the instrumentation
scope).

## The write-once producer layout for Langfuse v4

Langfuse v4 in its default write mode takes observations over **OTLP only** (the legacy ingestion
events for observations are refused) and makes a trace **be its root observation** (the trace list
is the list of root observations). On the measured 4.38.0 `events_only` receiver, a re-sent
observation id is an update, last write wins, even when its content or `environment` changes.
Transient rows during ingestion converge; they are not permanent duplicates. The observer detects
the receiver's family once per run and, on v4, writes a complete bundle-derived record once; live
previews are opt-in. Writing that record once is a producer policy, not a receiver limitation, and
it is what Langfuse asks of an OTLP producer on v4: one complete span per unit of work, never
exported again under its id
([Migrate custom ingestion to Langfuse v4](https://langfuse.com/integrations/native/opentelemetry/migration-to-v4)).

**How the family is decided.** `GET /api/public/v2/observations` answers on a v4 receiver in every
write mode and 404s on a v3 one; the version the receiver reports cannot decide, because a v4
receiver reports `4.x` in its transitional write modes too. The probe is read-only and runs once,
at run start, next to the project check; `options.langfuse.server_api` (`auto` / `v3` / `v4`)
overrides it, a receiver that cannot be asked leaves the run on the v3 family, and the family
lands in the tracing receipt (`details[0].server_api`).

**What the v4 family writes.**

| When | What | Ids |
|---|---|---|
| trial start | previews only: the **preview root** `preview: trial`, whose parent is the final root's id, with the trace name, session, the tags known then, the native fields and the identity metadata | `obs\|<trace>\|proot\|-` |
| every call end | previews only: the same live bodies as on a v3 receiver, under the **preview kinds** and under the preview root, named `preview: ...`, with `preview: true` in their metadata; a preview generation states zero usage and cost (explicitly, so the receiver infers none from its model) and its own figures in metadata (`prompt_tokens`, `completion_tokens`, `cost`, `cost_basis`), so it adds nothing to the trace's cost (§ Cost on a trace) | `pgen`, `pjgen`, `ptool`, `pjtool` |
| trial persisted | the whole bundle projection converted to spans by `tolokaforge_langfuse.otlp_spans`, written **once**, the **root last**, after the media upload, with the complete manifest in the root's metadata as a JSON string the receiver parses back; the scores through the ingestion route, each with the grading's own timestamp | the final kinds, unchanged |
| run end | one minimal **error root** for every trace whose real root can no longer come (the trial never persisted, the bundle pass wrote none, or the root never reached the exporter): name, session, tags, native fields, identity, start, `status: error` and the reason, no manifest and no verdict | `root` |

**Previews are opt-in.** By default nothing is written while a trial runs: the trial appears,
complete, when it is persisted (a trial that never persists appears at run end, as its error
root). `LANGFUSE_TRACING_PREVIEWS=true` (or `1` / `yes` / `on`) adds the
preview rows, to watch a long trial as it runs. A preview stays in the trace beside its final row
(a single observation cannot be deleted), and the receiver's own views count every row it holds:
its dashboards count each previewed call twice in their call and observation counts and latency
distributions. Usage and cost stay right, because a preview states zero (§ Cost on a trace). A v3
receiver ignores the switch: its live rows are the record. The receipt says whether previews
went out (`details[0].previews`: `on` / `off`, always `off` on v3).

Within a run, the producer does not re-send observations: a preview id can never collide with a
final one (the kind is part of the id), a preview row says so in its own metadata, and a reader
excludes previews by that marker and by the ids the contract derives. Until the final root arrives
the trace has **no** root row, so it is in no trace list; with previews on, a reviewer reaches a
running trial by its (deterministic) trace id or by its session, and the trace joins the list when
the trial ends.
The trace's name, session, tags, native
fields and identity metadata ride on **every** span, previews included, because a v4 receiver
stores and filters them per observation.

The receipt gains four counters under `extra`: `langfuse.previews_sent` (0 unless previews are on),
`langfuse.final_observations_sent`, `langfuse.error_roots_sent` and
`langfuse.roots_unconfirmed`. The first three count spans **queued**, not spans a receiver
acknowledged: the queue takes a span whether or not the endpoint answers, and what actually left
is `spans_exported` / `spans_dropped` at the top of the receipt. `projection: full` is required on
this family - the trace's root observation comes from the bundle - and a run that asks for less is
refused at run start.

**One POST per batch, and what happens when one fails.** The stock OTLP exporter re-posts a batch
that failed with a connection error or a retryable status. The v4 producer makes one POST attempt
per batch to avoid unnecessary requests and unintended overwrites, and posts the same bytes again
only after a refusal that normally comes before the receiver reads them: a gateway's own
refusal page, 429 or 503 (§ Retries). This does not guarantee
delivery or prevent an undeletable duplicate. It takes more than disabling the exporter's retry
loop: the SDK posts a second time on a lost connection (`_export` up to OpenTelemetry 1.44, its
OTLP client from 1.45), `requests` follows a 307 or 308 by re-sending the body, and a session's
adapter can retry by itself. So this exporter does not use the SDK's: it encodes the batch with
the SDK's public OTLP encoder and makes the one POST itself, through a `requests` session of its
own, with redirects refused and no adapter retries. It reads nothing the SDK keeps private: the
encoder's public module is all it takes from the SDK's exporter packages. Only 2xx responses are
successful; 3xx responses, including 307 and 308, are failed exports. An install that cannot build
the request fails the run at start rather than silently enabling retries. The receiver's headers
come from the caller alone, so a run without them, or with a credential provider only the SDK's
exporter would load, fails at start too rather than sending every batch to be refused. A v3 run
keeps the stock retrying exporter. The consequences are visible in the receipt:

- a batch the queue never took (it was full, or the flush budget ran out) is certainly unwritten,
  so the trace gets its **error root** at run end;
- a batch the exporter posted and could not confirm (after the retries § Retries allows) is
  **ambiguous**: no error root is written for it, because a minimal error root could overwrite a
  complete root already stored. The run warns, counts it in `langfuse.roots_unconfirmed`, and the
  offline uploader completes such a trace later (it reads which ids the receiver already holds
  before writing). A root whose batch was still waiting out a refusal when the error roots were
  decided, and had not landed when the run ended, is counted the same way.

The error root itself carries nine of the trace metadata schema's keys, not the full 34: it is
deliberately minimal (identity, status, the reason, the label and the time source), so a reader
that groups by `model_name` or by a verdict key does not see the failed trials at all. Look for
`status: error` or the `error_root` marker in the observation's own metadata.

**Where the verdict lives on this family.** The producer writes the trace's metadata once with the
root and does not rewrite it for later gradings: `pass`, `score` and `primary_grading` in the
metadata remain as of that write. This is the layout's policy, not a receiver immutability guarantee.
Scores carry the current answer instead. Beside the trace-level mirror of the primary grading the
observer writes a categorical `primary_grading` score naming the grading the mirror belongs to,
and both carry `scope: primary` in the score metadata. A reader that wants "the verdict as it
stands" queries the scores filtered on that marker; without the filter the grading-scoped copies
are counted too. The offline connector moves
the same pair when a later grading becomes primary.

The decision, the options it was chosen over and its consequences are
[ADR-0048](adr/0048-write-once-observations-append-only-receiver.md).

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

## Cost on a trace

A generation's cost (`costDetails.total`) is what was actually spent where the bundle says so:
the charge the provider's response stated for the call (`metrics.yaml`
`usage.calls[*].billed_cost_usd`, see
[LLM_LAYER.md § Billed cost](LLM_LAYER.md#billed-cost)), and the eval's own `cost_usd` where
no charge was stated. The generation's metadata names which (`cost_basis`), so a reader tells
an actual charge from an estimate (`tolokaforge_langfuse.costs`, the same rules in the offline
connector):

| `cost_basis` | `costDetails.total` is |
|---|---|
| `billed` | the charge the response stated |
| `litellm` | litellm's figure: a charge the response stated (OpenRouter's `usage.cost` in a bundle from before the charge was recorded, a LiteLLM gateway's response-cost header) or litellm's own price map; the bundle does not say which |
| `list` | the engine's pricing table |
| `eval` | the eval's figure, its source not recorded (the judge's aggregate, or a `cost_source` value this producer does not recognise) |
| `cli` | an agent transcript's turn: its share of what the agent's CLI reported the run cost (Claude Code's `total_cost_usd`), shared out by the turns' tokens at Claude's relative list prices |
| `none` | no figure at all: no call is paired, or the call states neither a charge nor an eval figure (`cost_source: unknown`, a route nothing could price); the cost is an explicit zero, so the receiver prices nothing from its own model table, and a paired call keeps its usage |

The judge generation that holds `grade.judge_usage` follows the same rule with the judge's
`billed_cost_usd`, the sum over its calls. Every other call in `usage.calls` counts on exactly
one generation:

- an agent turn pairs with one of the agent's calls and a simulated user turn with one of the
  user simulator's (`role: user`), by generation id, else positionally within the role when the
  turn and call counts agree (the engine stamps no generation id on a user message, so a user
  turn pairs positionally); a dialogue the simulator ended with the stop token alone
  (`stop_with_text: deliver`) has one simulator call more than turns, its last, so there the
  leading calls pair;
- a call no message pairs with (a resample the loop discarded, a call the pairing cannot place)
  gets a generation of its own at the trial's end, `<actor> call (no message)` (`agent call ...`, `user simulator call ...`; the position as
  `call_index`), with the key
  `call:<i>` under `gen` or `ugen` (`<i>` is the call's position in `usage.calls`);
- a turn without a call states zero usage and cost with `cost_basis: none`, so a receiver that
  merges an update into a live row keeps no figure of its own.

Langfuse adds a trace's generation costs into the trace's cost, so with `projection: full` the
trace's cost is the cost of every call the bundle records: the agent's, the user simulator's and
the judge's (`gradings: false` writes no judge generation, so then the judge's cost is not on the
trace). A call the bundle never records is not on the trace either: a user reply the reply guard
rejected, an auto-anchored warm-up that failed, possibly an attempt that timed out after the
provider billed it. On the v4 write-once layout a preview generation states zero usage and cost,
its own figures in metadata, so each call counts once, on its final row; a trace whose final rows
never arrive (the trial never persisted, or every final batch was lost) therefore shows 0 USD (a lost
batch among several leaves the cost short), and
an offline upload restores its cost only where a bundle exists. The trace carries no total of its
own; its metadata `cost_usd` stays the eval's figure (the agent's and the user simulator's calls,
priced as the eval priced them). `projection: gradings` sends no call records, so there the
trace's cost covers the live agent turns and the judge. A bundle written before the engine
recorded the charge keeps its eval cost on every generation, with the basis it names.

## The trace vocabulary

What a trace can be filtered by is fixed once, for both producers, in
`tolokaforge_langfuse/vocabulary.py` (the offline uploader imports the same module): a closed list
of tag prefixes with a producer / caller split, the value lists the engine's own output format
defines, and the tags the producer derives from the bundle and from the model-name normalizer.

| Who sets it | Prefixes |
|---|---|
| the producer, from the bundle and the resolver | `harness:tolokaforge`, `source:trial`, `task:<task id>`, `model:<vendor/model>`, `model_vendor`, `model_family`, and when the normalizer's rules derive them `model_generation`, `model_tier`, `model_variant`, `model_size`, `model_stage`, `model_snapshot`; from `task.yaml` `model_config.agent.reasoning` `reasoning_mode`, `reasoning_effort`, `reasoning_budget`; `route` (the provider the run config routed the agent's calls to) |
| the launcher that owns the receiver | `project:<the verified project, in a tag's spelling>` |
| the caller (config `tags`, `TOLOKAFORGE_TRACING_TAGS`, the profile's fixed tags) | `team`, `dataset`, `run_kind` (`eval`, `smoke`, `canary`, `test`, `probe`), `scope` (`full`, `sample`), `config`, `domain`, `ci_run`, `ci_chain` |

A caller tag under a producer prefix, an unknown prefix or a value outside a closed list is a
configuration error at run start: the receiver merges tags as a set and never removes one, so a
tag nobody can query by would be permanent. Nothing is invented: a facet the rules did not derive,
a reasoning setting the config did not make, yields no tag. The receiver's native `environment`
follows the vocabulary's rule unless a profile or `LANGFUSE_ENVIRONMENT` says otherwise:
`production` for `run_kind:eval`, `development` for everything else.

## The deployment profile

Everything a deployment decides about its traces, and neither producer may know as a value, is
one **profile**, and it lives in the tolokaforge run configuration next to the rest of the
deployment's settings: inline in the Langfuse block, normally once for every run under
`run_defaults` of the deployment's `project.yaml` (`docs/PROJECTS.md`). The block may instead name
a TOML or YAML profile file; `TOLOKAFORGE_TRACING_PROFILE` names one when the block names none,
and the offline connector's `--tag-profile` overrides it for one upload. The live observer reads
the block from the engine's merged run config, the offline connector reads the same block from
`project.yaml` through the wheel's engine-free reader, so both producers apply one document
(`tolokaforge_langfuse/src/tolokaforge_langfuse/profile.py` validates the profile,
`preflight.py` reads the block). Neutral example:

```yaml
# project.yaml at the deployment's root
name: pilot
run_defaults:
  observability:
    tracing:
      options:
        langfuse:
          model_name_normalizer: toloka
          profile:
            schema: 2
            version: pilot-2026.10.01.1          # joins the native `version` field
            tags:
              fixed: [team:pilot]                # every trace carries them; a caller may not contradict them
              # derived: [model_facets, reasoning, route]   # the bundle-derived groups (default: all)
              values:                            # closed lists for caller prefixes
                dataset: [v1, v3]
                domain: [billing, support]
              required:                          # beyond the vocabulary's required set
                trial: [dataset, scope, domain, config]
            derive:                              # the offline command's --derive (fnmatch, first match)
              scope: {full: full, sample: sample}
            metadata:
              keys: [campaign]                   # the per-run metadata keys a caller may set
              # fixed: {deployment: pilot}        # metadata every trace carries
            models:
              rules: deploy/model_name_rules.toml  # selects the toloka normalizer with these rules
            trace:
              name: "{dataset}/{domain}"         # a trial trace's name over its tag values and {label}
              user: model                        # none (default) | model: the agent's model identity
          project: pilot                         # the one receiver project the credentials must open
          project_id: pilot-project-id           # its id, where a launcher can compare it
          environments:                          # the project's native environments
            test: {accepts: [trial]}
            test-automation: {accepts: [transcript]}
            production: {accepts: [trial]}
            production-automation: {accepts: [transcript]}
```

A profile file carries the same keys (TOML: `schema = 2`, `[tags]`, `[tags.values]`,
`[derive.<prefix>]`, `[metadata]`, `[models]`; YAML: the mapping above), and schema 1 files
(environment, fixed tags and metadata, models) still load. A deployment without an
`environments` block may also give an `[environment]` rule: a `literal`, or `from_tag` a prefix
with `values` and a `default` (the vocabulary's own rule is `production` for `run_kind:eval`,
`development` otherwise).

**Names, never values.** The block holds names and paths only: the credentials, the base URL and
a gateway's header stay in the environment and are read through the `SecretManager`. The offline
reader refuses a `${...}` placeholder inside the block, because it reads the file as written.

**The project and its environments.** `project` is the one receiver project of the deployment:
the credentials must open it (checked before the first export, the fail-closed check above), it
is the default of `expect_project` and it gives the trace its `project:` tag. The check compares
the name exactly as the receiver shows it; the tag spells it lowercased, each run of whitespace
one `-`, because a tag value holds no whitespace (`Toloka Arena` gives `project:toloka-arena`, and
`pilot` stays `project:pilot`). Capitals are lowercased even without a space (`Toloka` gives
`project:toloka`), so one project keeps one tag whatever the case of its name. A launcher variable
(`TOLOKAFORGE_TRACING_EXPECT_PROJECT`, `LANGFUSE_PROJECT`) naming a different project is a
configuration error. `environments` declares the project's native environments and what each
accepts: `trial` (benchmark data), `transcript` (an agent's own transcript) or `any`. With the
block declared, `LANGFUSE_ENVIRONMENT` is the selector and it is required: it must name a declared
environment, and that environment's `accepts` must admit a trial, or the run is refused at start.
The block's `environment` literal is refused next to `environments`, and a profile's environment
rule is not used there (a warning says so). `tracing_receipt.json` records the environment and
the profile version in the `details` entry.

**One anchoring rule.** Every relative path in the block (a profile path, the inline profile's
`models.rules`, `model_name_rules`) anchors to the directory of the `project.yaml` that supplied
it. The live observer receives the merged block without the file it came from, so it walks up
from the working directory to the nearest `project.yaml` (the engine loader's walk: the start
directory and eight parents), else it uses the working directory; the offline connector anchors
to the file it reads. A file named by `TOLOKAFORGE_TRACING_PROFILE` keeps its own directory for
its `[models] rules`.

**Layering.** `project.run_defaults` merges under the run config: maps key by key, lists replace.
So a tag every trace of the deployment carries belongs in the profile's `fixed` list, never in
`run_defaults`' `tracing.tags`, which a run config's own `tags` would replace; a run config may
add `tags` (a generator can write each config's `domain:` tag there). A run config that changes
the Langfuse block makes the two producers read different documents: keep the block in
`project.yaml` alone. The project loader ignores unknown keys below the top level, so a misspelt
parent key (`run_default:`, `observabilty:`) drops the whole block silently; the preflight below
fails closed on that.

**The preflight.** `python -m tolokaforge_langfuse.preflight --config <run config>
[--environment <name>] [--tags a:b,...] [--metadata k=v,...]` layers `project.run_defaults`
under the run config the way `tolokaforge run` does, computes the plan the live observer would
run under (the tags with their origins, the metadata keys, the environment, the project, the
profile and model-rules versions, the native `version`) with the plugin's own code and no
network, prints it and exits 2 on the first error: no block, no `project`, no `environments`, an
undeclared environment or one that accepts no trial, a tag conflict, a missing required tag, a
metadata key outside the profile's list, or an engine without the trial-observer seam (a pin older
than 0.27.0). It warns on an `options` namespace no installed plugin claims. `--offline` needs no
engine: over a `project.yaml` it reads the block alone (the offline connector's view), over a run
config it layers the nearest `project.yaml`'s tracing section under the run config's with the
engine's rule, so a config's own tags are checked where no engine is installed. A CI launcher runs
it over the exact config file the run receives and degrades to an offline upload rather than
failing the run. `python -m tolokaforge_langfuse.profile <file> [--tags ...]
[--metadata ...]` still validates a single profile file.

The producers validate the shape and apply the profile mechanically. A profile that does not
load, an environment outside the receiver's alphabet (lowercase letters, digits, `-`, `_`, at most
40 characters, never starting with `langfuse`), a fixed tag under a producer prefix, two values
under one prefix, a value outside a closed list or a metadata key the projection writes itself is a
configuration error at run start. Under a profile the required set is enforced at run start too
(the vocabulary's `team`, `run_kind`, `dataset`, `scope` plus the profile's), the same rule the
offline uploader applies before it uploads.

**Per-run values from the launcher.** `LANGFUSE_ENVIRONMENT` selects the environment (with
`environments` declared) or overrides the profile's rule and the config's `environment` (without);
`TOLOKAFORGE_TRACING_METADATA` (`key=value,...`) carries the per-run metadata the offline command
receives as `--metadata` (profile fixed keys < config `metadata` < the variable; a key of the fixed
schema, or outside the profile's `keys`, is refused). The Langfuse connector's
`with-environment <name> --config project.yaml -- tolokaforge run ...` injects the receiver, the
environment and the expected project; the child engine reads the profile from its own run
configuration, so one launcher serves a local run and the CI.

**Native fields.** `environment` rides on every span (the receiver fixes a trace's environment at
the first write it sees, verified on Langfuse 3.205.1 on 2026-09-17: a trace written without it
reads `default` and no later update repairs it) and on every ingestion body of the trial-end pass
(observations and scores are filed under `default` otherwise, whatever the trace says);
`release` is the engine's own version (`tolokaforge-<version>`, also written into
`run_identity.json` as `engine_version` for the offline uploader); `version` is the producer's
identity plus the model-name rules and the profile it ran under
(`tolokaforge-langfuse-<version>+<rules version>+<profile version>`; the offline uploader writes
`langfuse-connector-<version>+...`).

## Packaging: the seam in the engine, the observer in its own wheel

The engine owns the seam and nothing receiver-shaped: `tolokaforge/observability/observer.py`
(the `TrialObserver` hooks, null, in-memory and composite observers, and the receipt), `ids.py` (the id
contract shared with the offline uploader) and `factory.py` (the run identity, `run_identity.json`,
`tracing_receipt.json`, and the discovery of the installed **trial-observer plugins**). Everything
Langfuse-shaped is the `tolokaforge-langfuse` distribution (`tolokaforge_langfuse/` in this
repository, a workspace member released on its own `langfuse-vX.Y.Z` cadence, `docs/RELEASING.md`):
the OTLP observer, the trial-end projection, the gradings pass, the media and ingestion calls, the
attachment manifest, the deployment profile, the model-name resolution, and the coding-agent
transcript path (`transcripts.py`) with the outbound sentinel its uploaders scan with
(`safety.py`).

A plugin is a callable registered under the `tolokaforge.trial_observers` entry-point group with the
signature `build(tracing, identity, *, engine_run_id, output_dir) -> TrialObserver | None`. At run
start the engine resolves the identity, asks every installed plugin in name order and composes the
answers; `None` means "nothing asks for me in this run" (the Langfuse plugin answers `None` unless
`exporter: otlp` or `LANGFUSE_TRACING_ENABLED` asks). `exporter: otlp` with no plugin answering,
or a plugin that cannot be imported, is a configuration error at run start; so is a plugin's switch
(a variable ending in `_TRACING_ENABLED`, such as `LANGFUSE_TRACING_ENABLED`) that is on while no
plugin produced an observer, so a run never proceeds silently without the traces it asked for. The
pairing is checked
by the plugin: the engine's `PLUGIN_API_VERSION` (the `build` signature, the observer hooks and the
id, configuration and receipt contracts, currently version **4**) must equal the plugin's `__api_version__`, and a mismatch names both versions. So a
fix to the projection, the profile or the attachment step reaches a deployment by moving the
`tolokaforge-langfuse` pin while the engine pin stays; a change to the hooks or the ids moves both,
engine first. `observability.tracing.options.<plugin>` carries receiver-owned settings.
Unknown top-level tracing keys are rejected at config load, so misspellings cannot silently
switch off a requested setting.

`TrialObserver` is an in-process hook: it receives engine objects such as `GenerationResult`
and `Trajectory`. A remote collector needs an in-process plugin that translates those objects
into its transport. The durable cross-process contract is the deterministic id scheme plus the
persisted bundle, not the Python hook arguments. `InMemoryTrialObserver` provides a deterministic
fixture with `call_log.calls`, a configurable `receipt`, and per-hook exceptions in `fail_on`.

## Delivery

Spans go through a bounded queue exported by a background thread (`queue_size`,
`export_batch_size`, `export_interval_s`); a full queue drops spans and counts them, the agent
loop never waits. At run end the queue is flushed within `flush_timeout_s` and
`tracing_receipt.json` in the run directory reports `spans_queued`, `spans_exported`,
`spans_dropped`, `export_failures`, `flushed`; the same counts go to the log (a warning when
anything was dropped). If the receiver is unreachable the flush gives up after `flush_timeout_s`
and counts the rest as dropped, so a run never waits on its traces, also while the background
thread still exports a batch: a batch it holds when the run ends counts as posted and not
confirmed. `flushed` is false only when something was dropped or a batch was still being
exported; a batch that finished just past the timeout has left. The flush goes on longer only
while the receiver refuses with an answer the retry policy waits out, by `retry.flush_grace_s`
at most, and stops going on once a batch fails in another way (a timeout, a lost connection, a
status the policy does not wait out) or the retry breaker opens (§ Retries).

**What leaves is scanned, by one gate per run.** Tool-call **arguments** (a mapping) pass through
the engine's `SensitiveKeyRedaction`, which reads key names only; tool outputs and message text are
free text it cannot cover, so they are capped at `attribute_max_chars` and scanned: every live span
(its name and attributes) before it is queued, each trial-end pass (the bundle projection and the
gradings) before it is sent, and the attachments' files before they are uploaded. A pass is scanned
with the observer's gate whatever the attachment step offers, so a step with no scanner of its own
sends nothing unscanned.

- **What the gate knows.** The credentials the process holds: its environment (names that look
  secret-like), the `SecretManager`'s keys and the receiver's header values, each by the name of the
  variable it came from. The `SecretManager` is read again at a trial's start when it was replaced
  meanwhile (`register_runtime_secret`: the engine's generated TypeSense key), so a secret registered
  after the observer was built is known from the next trial on.
- **What it leaves out.** A value the run's own tracing values contain (the session, run id and tag,
  label, tags, metadata, environment, release) rides on every span by design: `ACME_TOKEN=tolokaforge`
  against the tag `harness:tolokaforge`. One such variable would withhold the whole run, so the gate
  leaves the value out and logs a warning naming the variable, never the value.
- **What is looked for, and where.** The key-shaped patterns of § Attachments run over the span (or
  the events) serialised as JSON, as they always did over the bundle's events. The credential values
  run over the serialised JSON and over every raw string in it, in the forms JSON gives them: JSON
  escaping hides a credential that holds a quote or a backslash, an attribute that is itself JSON
  text (a generation's input and output, a tool's input) escapes it once more, and a tool's own JSON
  may write non-ASCII characters as `\uXXXX`. The patterns do not run over raw strings, where a
  line-anchored one would stop ordinary code such as `api_key = os.environ.get(...)`. The
  trade-off: a credential whose value the gate does not know, inside JSON text and with no provider
  shape, can pass; one it knows is matched in every form.
- **What a hit does.** It withholds what would have carried it, whatever it is (a generation, a tool
  row, a preview, a root, an error root, a pass, a file): nothing is rewritten, the warning names the
  span or trace, the rules and the variables, never a value, and the receipt counts it. A withheld
  span is neither queued nor dropped; a withheld pass is `projections_refused_secret` or
  `gradings_refused_secret`, not `*_failed` (a scan that could not run, a malformed bundle and a
  refused batch are); a withheld file is `attachments_skipped`. At run end one warning sums up what
  was withheld, by rule and variable.
- **Limits.** A text longer than `attribute_max_chars` is scanned as capped, so a credential the cap
  cuts is not recognised. Base64 image blocks never leave through spans.

The receiver's headers are read through the `SecretManager` (`OTEL_EXPORTER_OTLP_HEADERS`) so their
value is redacted from the engine's logs.


The receipt is a strict Pydantic `ExportReceipt`: its common fields are `spans_queued`,
`spans_exported`, `spans_dropped`, `export_failures`, `flushed`, and `exporter`. Plugin counters
live under `extra`, with namespaced keys such as `langfuse.projections_sent`. Non-additive
receiver facts live in `details`, for example:

```json
{"exporter": "langfuse", "expect_project": "pilot", "project_verified": "verified",
 "server_api": "v4", "previews": "off", "environment": "test",
 "profile_version": "pilot-2026.10.01.1"}
```

`details` is a list, preserving each observer's facts even when two target different projects.
`CompositeTrialObserver` sums common and plugin counters, ANDs `flushed`, and concatenates
`details` without interpreting receiver keys. A missing receipt counts as an export failure
and leaves `flushed: false`.

Every counter the Langfuse plugin reports under `extra` (each key has the `langfuse.` prefix):

| Counter | Counts |
|---|---|
| `attachments_registered`, `attachments_uploaded`, `attachments_deduplicated` | files registered with the receiver, uploaded, and found already stored |
| `attachments_skipped`, `attachments_failed` | files kept back (a data-safety hit, a file type) and files that could not be sent |
| `manifests_sent`, `manifests_failed` | manifest v2 sent (on v4, carried by the root) and not |
| `projections_sent`, `projections_failed`, `projections_refused_secret` | bundle projections sent; failed (a bundle that could not be projected or scanned, a tripped breaker, a refused batch); withheld by the gate |
| `observations_sent`, `events_sent`, `media_uploaded`, `media_failed` | what the sent projections held |
| `gradings_sent`, `gradings_failed`, `gradings_refused_secret` | the grading pass, counted like a projection (a grading whose scores did not reach the receiver is a failure) |
| `scores_sent`, `user_generations_sent` | the scores and simulated user turns the grading passes sent |
| `previews_sent`, `final_observations_sent`, `error_roots_sent`, `roots_unconfirmed` | the v4 layout (§ The write-once producer layout) |
| `spans_refused_secret` | live spans the gate withheld |
| `retried_requests`, `retry_attempts`, `retries_recovered`, `retries_exhausted`, `retry_wait_s`, `retry_breaker_trips` | the retry policy over every write route (§ Retries): requests posted more than once, the posts after the first, retried requests that landed, requests still refused when the policy, a deadline, the breaker or the run's end stopped them, the seconds waited (rounded up), and how often the breaker stopped the waiting |

A receipt covers one process. `ExportReceipt.merge` applies the same reduction to receipts
collected from distinct workers; the caller must deduplicate workers and retain partial/final
status before calling it. The engine does not collect worker receipts automatically. Counts
measure export attempts, so deterministic ids prevent duplicate receiver records without making
repeat receipt merges idempotent.

## Retries

A receiver can sit behind a gateway that answers a shared write limit with its own block page: a
403 the gateway sends without forwarding the request, in bursts that can last minutes while other
clients spend the same limit. The receiver never reads a body refused this way, so the writes
follow one retry policy, `options.langfuse.retry` (`tolokaforge_langfuse.retry`).

**What is posted again.** Only a refusal that comes before the receiver reads the body
(ADR-0048, amendment 2026-10-07):

| Answer | Why the body was not read |
|---|---|
| 403 whose body carries one of `gateway_markers` | the gateway in front of the receiver refused it without forwarding it (the offline connector recognises the Azure Application Gateway's page the same way) |
| 429 | a limit refuses before processing |
| 503 | a service answers it before it processes the request; a proxy may also answer it after forwarding the request, and on a v4 receiver the second post is then an update with the same content |

A 403 Langfuse answers itself is JSON and fails at once: a key Langfuse rejects is never waited
on. The gateway's page does not say why it refused, though: a rule of its own (an admission header
it checks itself, missing or rotated) looks exactly like its shared limit and is waited out too,
until the breaker below stops the waiting. A lost answer and a timeout get no second post (the
receiver may have taken the body), and neither does any status the list does not name. An
operator may list more statuses, but listing 500, 502 or 504 can re-send a body the receiver has
already read and written: a gateway answers those when the receiver broke off or did not answer
in time. On a v4 receiver that second write is an update with the same content (same ids, same
rows, last write wins), which is the trade-off ADR-0048 describes.

**The schedule.** Every retried answer waits the same steps: the wait before the n-th re-send is
the n-th value of `delays_s`, and the last value repeats once the list runs out, for at most
`max_retries` re-sends. With the defaults that is 1, 3, 9, 20, 30 and 30 s, 93 s in all, so at
most 7 posts. A `Retry-After` longer than the step is honoured instead, up to
`retry_after_max_s`, and the jitter then lengthens the wait.

**The settings**, every key optional, each default with its reason:

| Key | Default | Why |
|---|---|---|
| `statuses` | `[403, 429, 503]` | the refusals that come before the receiver reads the body (above; 503 with the exception its row names); 403 stands for the gateway's page alone; `[]` turns retries off |
| `gateway_markers` | `[Microsoft-Azure-Application-Gateway]` | the text that marks a 403's body as the refusal page of the gateway in front of the receiver. The default is the text of the Azure Application Gateway's page; a deployment behind another gateway names the text of its page. A 403 that carries none of them is the receiver's own and fails at once |
| `delays_s` | `[1, 3, 9, 20, 30]` | a refusal that passes in seconds is caught by the short first waits; the steps grow about threefold up to 30 s, which repeats, so a lasting refusal costs about two posts a minute; a value that is not a positive finite number, or that is shorter than the one before it, is refused |
| `max_retries` | `6` | the most re-sends of one request after its first post: waits of 1 + 3 + 9 + 20 + 30 + 30 = 93 s before the jitter, which fit inside the default 120 s `attach_budget_s`, so a trial-end call can run the whole schedule; a refusal that lasts longer than about 93 s still costs the batches it refuses throughout; `0` turns retries off |
| `jitter` | `0.2` | each wait grows by a random 0 to 20 % of itself and never shrinks: a run's parallel trials are refused together and would otherwise post again together, while a `Retry-After` never ends early |
| `retry_after_max_s` | `60` | a `Retry-After` longer than the step is honoured up to a minute; a receiver asking for more would hold a trial or the run's end for it |
| `breaker_after` | `2` | two span batches in a row that ran out their whole schedule still refused (about three minutes of refusals) point to a rule rather than the limit, so a refusal then fails at once until a write is accepted again; `0` never stops the waiting |
| `flush_grace_s` | `240` | how much longer than `flush_timeout_s` the run's end may wait while the receiver refuses: two whole schedules with the jitter (2 x 112 s) and their posts fit, so a burst of about three minutes at the run's end is ridden out |

```yaml
# project.yaml
run_defaults:
  observability:
    tracing:
      flush_timeout_s: 30                 # the engine's run-end flush bound
      options:
        langfuse:
          retry:
            statuses: [403, 429, 503]     # 403: the gateway's page only
            gateway_markers: [Microsoft-Azure-Application-Gateway]
            delays_s: [1, 3, 9, 20, 30]   # the last value repeats
            max_retries: 6                # re-sends after the first post; 0 turns retries off
            jitter: 0.2
            retry_after_max_s: 60
            breaker_after: 2
            flush_grace_s: 240
```

An unknown key or a bad value (a status outside 400-599 or listed twice, a blank gateway marker,
no gateway marker while `statuses` lists 403, a `max_retries` that is not a whole number from 0
to 100, an empty or shrinking `delays_s` or one with a value that is not a positive finite
number, a negative or non-finite number elsewhere, a jitter above 1, a boolean where a number
belongs) refuses the run at start. The offline connector reads the same
block through the wheel's reader, so a deployment that sets `retry` needs a connector whose wheel
knows the key.

**Where it applies, and what bounds it.**

- **The span export of the write-once layout** (a v4 receiver). The background thread posts a
  refused batch again, the same bytes each time, and the trials never wait for it. A v3 receiver
  keeps the SDK's exporter and its own retries (429, 502, 503, 504; not the gateway's page).
- **The trial-end calls** of either family: the media registration, its confirmation and the
  presigned upload, the manifest, the gradings and the scores. They run in the trial's own
  thread, and a wait starts only if it ends, with a second to spare for the request, before the
  trial's `attach_budget_s` runs out; each request gets what is left of the budget. The whole
  default schedule (93 s) fits the default budget, so the first call of a pass that meets a
  refusal can wait it out to the end; calls after it in the same pass have only what is left. A
  deployment that would rather ride out a longer refusal at the trial's end raises
  `attach_budget_s` and `max_retries`, at the cost of the trial's wall time.
- **The breaker.** The span export's requests count towards `breaker_after`: once that many in a
  row ran out their whole schedule still refused, a refusal fails at once, without a wait, until
  the first write accepted afterwards closes it. The trial-end calls obey it but do not count
  towards it, since a run's parallel trials would otherwise open it within one schedule of any
  long refusal. A gateway rule that refuses for good therefore costs the span export two
  schedules in its background thread, and each trial end its schedule until the breaker opens
  (or the attachment step's own breaker switches the step off after three trials that reached
  nothing). Opening the breaker also ends the waits in progress. On a v3 receiver nothing counts
  towards it, since the span export keeps the SDK's exporter: there a gateway rule that refuses
  for good costs each trial end its schedule within `attach_budget_s`, until the attachment
  step's own breaker switches the step off. The transcript upload has a breaker of its own.
- **The run's end.** The flush ends at `flush_timeout_s` unless the receiver refuses it with an
  answer the policy waits out (a batch already waiting one out when the flush starts counts too);
  then it may go on, by `flush_grace_s` at most in total over its flushes (two on a v4 receiver:
  before and after the error roots). It stops going on once a batch fails in another
  way (a timeout, a lost connection, a status the policy does not wait out) or the breaker opens.
  No wait ends past the grace, and whatever did not get out is counted (`spans_dropped`,
  `export_failures`), so a v4 run's end takes at most 2 x `flush_timeout_s` + `flush_grace_s`
  (300 s with the defaults), plus, per flush, the timeout of one request in flight.
  `flush_grace_s: 0` keeps every wait within the timeout.
- **The agent transcripts** (`automation langfuse-upload`) leave through the same transport with
  the default policy. All the upload's waits together take at most `flush_grace_s` (only the time
  spent waiting counts, so a long upload keeps its retries), and the report's `retries` holds the
  same counts as the receipt.

**What a run says.** Every retry is an INFO line naming the request (a method and a path, never
a URL or a header), the status, the wait and the retry's number; every give-up is a WARNING with
the status, the posts, the time waited and the reason (`max_retries`, the deadline, the wait
budget, the run's end). The breaker says once at WARNING that it opened, and at INFO that a write
was accepted again; a request it stops is an INFO line. The receipt counts it all over the routes
(§ Delivery): `retried_requests`, `retry_attempts`, `retries_recovered`, `retries_exhausted`,
`retry_wait_s`, `retry_breaker_trips`. Every other counter keeps its meaning: a batch that landed
on its second post is exported, not failed.
