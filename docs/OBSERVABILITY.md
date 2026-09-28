# Live tracing (ADR-0047)

`observability.tracing` in the run config selects an installed trial-observer plugin. The
Langfuse plugin described here exports each generation and tool call as an OpenTelemetry span
while the trial runs; the graded trial closes the trace. Its OTLP/HTTP exporter sends valid spans
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
    label: pilot_agent                     # trace name <label>/<task_id>
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
```

The engine's `TracingConfig` owns only exporter selection, endpoint, run identity, session/label,
service name, tags, metadata, queue/flush limits and span content limits. `exporter` may name any
installed plugin's supported exporter. `options` is an opaque mapping keyed by plugin name;
the engine passes it through unchanged. The Langfuse plugin validates `options.langfuse` against
its strict `LangfuseConfig` before contacting the receiver. All receiver-specific settings below
(`expect_project`, `project`, `project_id`, `environments`, `attach`, `gradings`, `projection`,
`attach_*`, `profile`, `environment`, `model_name_*`) live in that namespace. Defaults and environment precedence are unchanged.

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
| `LANGFUSE_PROJECT` | the project the keys must open (checked before the first export) and the trace's `project:` tag |
| `LANGFUSE_EXTRA_HEADERS` | `k=v,k2=v2`, extra request headers (a gateway's own header) |
| `TOLOKAFORGE_TRACING_RUN_ID`, `_RUN_TAG`, `_SESSION_ID`, `_LABEL` | the run's identity when the config carries none |
| `TOLOKAFORGE_TRACING_PROFILE`, `LANGFUSE_ENVIRONMENT`, `TOLOKAFORGE_TRACING_METADATA` | a profile file when the config names none, the environment (the selector when the config declares `environments`) and the per-run metadata (the profile section below) |

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` keep precedence when set.

This is how a deployment keeps its credentials out of its configuration: the Langfuse connector
(tolokaforge-tools, `langfuse-connector with-environment <name> --config project.yaml --
tolokaforge run ...`) reads the deployment's block, checks the keys open its project, and injects
the endpoint, the header, the attachment API base, the environment and the expected project into
the engine's environment, printing nothing. A config that names its own `endpoint` keeps it.

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

Before a file leaves, its bytes are scanned against the `SecretManager`'s credential values
(keys with secret-like names) and the key-shaped patterns of the connector's data-safety gate
(dotenv secrets, `Authorization` headers, PEM blocks, URL credentials, provider key prefixes,
JWTs, secret-named fields); a hit skips the file, names it in `attachments_skipped` and leaves
`attachments_complete: false`. Bytes are never rewritten. The REST base URL derives from the OTLP
endpoint (`attach_api_base` overrides it), the headers are `OTEL_EXPORTER_OTLP_HEADERS`, each
request has `attach_timeout_s`; the step runs in the trial's thread once the trial is over and
never raises. Under `extra`, `tracing_receipt.json` reports the `langfuse.`-prefixed counters
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
| `trajectory.yaml`, `metrics.yaml` | the root observation, one generation per agent turn with its paired usage and cost (by generation id, else positionally), one generation per simulated user turn (the user model, no usage), the trace's input, output, timestamp, status and totals |
| `tool_log.yaml` | one tool span per recorded call, the grader's view (status, executor, latency, sequence, untruncated output) with the transcript's agent-facing text beside it when it differs; the user simulator's own tool calls too |
| `grade.yaml`, `judge_trajectory.yaml`, `judge_inputs.yaml` | the `grading:live:<run_id>` observation with the judge turns beneath, its scores and the trace-level mirror (`gradings: false` leaves the grading out, like the offline `--grades none`) |
| `logs.yaml`, `trajectory.user_reply_guard_events`, `provision_stage`, the run's `LIMIT_HIT.json`, `services/_capture.yaml` | events (WARNING and ERROR always, INFO under `attach: all`) |
| `task.yaml`, `env.yaml`, `engine_run_state.json` | the metadata groups: task facts, the three model configurations with their presets and policies, the environment identity, the redaction stamp, the models fingerprint |
| base64 image blocks in messages | media registered on the observation, the token in its output (raw base64 never enters an ingestion body) |
| the attachment step | manifest v2, complete, in the same trace body |

`projection: gradings` sends only the grading, its scores and the user turns; `none` sends the attachments alone. The pass runs under the attachment
step's budget and breaker, the serialised events go through the same data-safety scan as the
files (a hit sends nothing and counts), and nothing raises into the trial. The receipt reports
the following counters under `extra`, each prefixed with `langfuse.`:
`projections_sent`, `projections_failed`, `observations_sent`, `events_sent`, `scores_sent`,
`gradings_sent`, `user_generations_sent`, `media_uploaded`, `media_failed`.

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
the receiver's family once per run and uses separate live previews and a complete bundle-derived
record on v4. Writing that record once is a producer policy, not a receiver limitation.

**How the family is decided.** `GET /api/public/v2/observations` answers on a v4 receiver in every
write mode and 404s on a v3 one; the version the receiver reports cannot decide, because a v4
receiver reports `4.x` in its transitional write modes too. The probe is read-only and runs once,
at run start, next to the project check; `options.langfuse.server_api` (`auto` / `v3` / `v4`)
overrides it, a receiver that cannot be asked leaves the run on the v3 family, and the family
lands in the tracing receipt (`details[0].server_api`).

**What the v4 family writes.**

| When | What | Ids |
|---|---|---|
| trial start | the **preview root** `preview: trial <task>/<trial>`, whose parent is the final root's id, with the trace name, session, the tags known then, the native fields and the identity metadata | `obs\|<trace>\|proot\|-` |
| every call end | the same live bodies as on a v3 receiver, under the **preview kinds** and under the preview root, named `preview: ...`, with `preview: true` in their metadata | `pgen`, `pjgen`, `ptool`, `pjtool` |
| trial persisted | the whole bundle projection converted to spans by `tolokaforge_langfuse.otlp_spans`, written **once**, the **root last**, after the media upload, with the complete manifest in the root's metadata as a JSON string the receiver parses back; the scores through the ingestion route, each with the grading's own timestamp | the final kinds, unchanged |
| run end | one minimal **error root** for every trace whose real root can no longer come (the trial never persisted, the bundle pass wrote none, or the root never reached the exporter): name, session, tags, native fields, identity, start, `status: error` and the reason, no manifest and no verdict | `root` |

Within a run, the producer does not re-send observations: a preview id can never collide with a
final one (the kind is part of the id), a preview row says so in its own metadata, and a reader
excludes previews by that marker and by the ids the contract derives. Until the final root arrives
the trace has **no** root row, so it is in no trace list; a reviewer reaches a running trial by its
(deterministic) trace id or by its session, and the trace joins the list when the trial ends.
The trace's name, session, tags, native
fields and identity metadata ride on **every** span, previews included, because a v4 receiver
stores and filters them per observation.

The receipt gains four counters under `extra`: `langfuse.previews_sent`,
`langfuse.final_observations_sent`, `langfuse.error_roots_sent` and
`langfuse.roots_unconfirmed`. The first three count spans **queued**, not spans a receiver
acknowledged: the queue takes a span whether or not the endpoint answers, and what actually left
is `spans_exported` / `spans_dropped` at the top of the receipt. `projection: full` is required on
this family - the trace's root observation comes from the bundle - and a run that asks for less is
refused at run start.

**One POST per batch, and what happens when one fails.** The stock OTLP exporter re-posts a batch
that failed with a connection error or a retryable status. The v4 producer makes one POST attempt
per batch to avoid unnecessary requests and unintended overwrites. This does not guarantee
delivery or prevent an undeletable duplicate. It takes more than disabling the exporter's retry
loop: the SDK's own `_export` posts a second time on a lost connection, `requests` follows a 307 or 308
by re-sending the body, and a session's adapter can retry by itself. The exporter makes the
request itself with redirects refused and no adapter retries. Only 2xx responses are successful;
3xx responses, including 307 and 308, are failed exports. An OpenTelemetry SDK whose exporter cannot
enforce this policy fails the run at start rather than silently enabling retries. A v3 run keeps
the stock retrying exporter. The consequences are visible in the receipt:

- a batch the queue never took (it was full, or the flush budget ran out) is certainly unwritten,
  so the trace gets its **error root** at run end;
- a batch the exporter posted and could not confirm is **ambiguous**: no error root is written for
  it, because a minimal error root could overwrite a complete root already stored. The run warns,
  counts it in `langfuse.roots_unconfirmed`, and the offline uploader completes such a trace later
  (it reads which ids the receiver already holds before writing).

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

## The trace vocabulary

What a trace can be filtered by is fixed once, for both producers, in
`tolokaforge_langfuse/vocabulary.py` (the offline uploader imports the same module): a closed list
of tag prefixes with a producer / caller split, the value lists the engine's own output format
defines, and the tags the producer derives from the bundle and from the model-name normalizer.

| Who sets it | Prefixes |
|---|---|
| the producer, from the bundle and the resolver | `harness:tolokaforge`, `source:trial`, `task:<task id>`, `model:<vendor/model>`, `model_vendor`, `model_family`, and when the normalizer's rules derive them `model_generation`, `model_tier`, `model_variant`, `model_size`, `model_stage`, `model_snapshot`; from `task.yaml` `model_config.agent.reasoning` `reasoning_mode`, `reasoning_effort`, `reasoning_budget`; `route` (the provider the run config routed the agent's calls to) |
| the launcher that owns the receiver | `project:<the verified project>` |
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
is the default of `expect_project` and it gives the trace its `project:` tag. A launcher variable
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
attachment manifest, the deployment profile and the model-name resolution.

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
and counts the rest as dropped, so a run never waits on its traces. Tool-call **arguments** (a
mapping) pass through the engine's `SensitiveKeyRedaction`; tool outputs and message text are free
text, which key-based redaction cannot cover, so they are capped at `attribute_max_chars` but not
redacted; base64 image blocks never leave through spans. The receiver's headers are read through
the `SecretManager` (`OTEL_EXPORTER_OTLP_HEADERS`) so their value is redacted from the engine's logs.


The receipt is a strict Pydantic `ExportReceipt`: its common fields are `spans_queued`,
`spans_exported`, `spans_dropped`, `export_failures`, `flushed`, and `exporter`. Plugin counters
live under `extra`, with namespaced keys such as `langfuse.projections_sent`. Non-additive
receiver facts live in `details`, for example:

```json
{"exporter": "langfuse", "expect_project": "pilot", "project_verified": "verified",
 "server_api": "v4", "environment": "test", "profile_version": "pilot-2026.10.01.1"}
```

`details` is a list, preserving each observer's facts even when two target different projects.
`CompositeTrialObserver` sums common and plugin counters, ANDs `flushed`, and concatenates
`details` without interpreting receiver keys. A missing receipt counts as an export failure
and leaves `flushed: false`.

A receipt covers one process. `ExportReceipt.merge` applies the same reduction to receipts
collected from distinct workers; the caller must deduplicate workers and retain partial/final
status before calling it. The engine does not collect worker receipts automatically. Counts
measure export attempts, so deterministic ids prevent duplicate receiver records without making
repeat receipt merges idempotent.
