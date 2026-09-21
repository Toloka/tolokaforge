# tolokaforge-langfuse

Langfuse live tracing for [tolokaforge](../README.md): the OpenTelemetry trial observer that
exports every generation and tool call of a trial as a span while the trial runs, and the
trial-end pass that completes the trace from the persisted bundle (metadata, observations,
gradings, scores, media, the attached files) under the id contract the offline
`langfuse-connector` shares. The engine's own documentation of the behaviour is
[`docs/OBSERVABILITY.md`](../docs/OBSERVABILITY.md); the decision record is
[ADR-0047](../docs/adr/0047-live-tracing-trial-observer-otel.md).

## Why a separate wheel

The engine owns the seam (`tolokaforge.observability.observer.TrialObserver`, the id contract in
`tolokaforge.observability.ids`, the run identity in `tolokaforge.observability.factory`) and
knows no receiver. Everything Langfuse-shaped lives here and releases on its own cadence
(`langfuse-vX.Y.Z` tags, see [`docs/RELEASING.md`](../docs/RELEASING.md)), so a fix to the
projection, the profile or the attachment step reaches a deployment by moving this package's pin
while the engine pin stays where it is. The pairing is checked at run start: the engine's
`PLUGIN_API_VERSION` (currently **4**, including the neutral config and receipt shapes) must equal this package's `__api_version__`, and a mismatch names both.

## Install

```bash
pip install 'tolokaforge[otel]'      # the engine's extra resolves to this package
pip install tolokaforge-langfuse     # or on its own, next to an installed engine
```

The engine discovers the observer through the `tolokaforge.trial_observers` entry-point group
(`langfuse = tolokaforge_langfuse.plugin:build`). Nothing else is registered; a run without
`observability.tracing.exporter: otlp` and without `LANGFUSE_TRACING_ENABLED` gets no observer.

## Configuration

The engine's `observability.tracing` block contains only common settings: `exporter`,
`endpoint`, run identity, session/label, service name, tags, metadata, queue/flush limits and
span content limits. Receiver settings belong to **`observability.tracing.options.langfuse`**:

```yaml
observability:
  tracing:
    exporter: otlp
    endpoint: https://langfuse.example/api/public/otel/v1/traces
    options:
      langfuse:
        expect_project: pilot
        attach: all
        gradings: true
        projection: full
        server_api: auto
        profile: deploy/langfuse_tracing.toml
```

`server_api` says which receiver family to write for: `auto` (the default) asks the receiver once
at run start, by capability, and a Langfuse v4 receiver gets the **write-once layout** (declared
preview rows while the trial runs, the record written once from the bundle, the root last); see
`docs/OBSERVABILITY.md`, "The write-once layout", and ADR-0048. Two consequences worth knowing
before configuring a run on that family: `projection` must be `full`, because the trace's root
observation comes from the bundle, and the current verdict lives in the `scope: primary` scores
(the trace metadata is frozen at its single write). `config.LangfuseConfig` also owns
`attach_api_base`, `attach_timeout_s`, `attach_budget_s`,
`environment`, `model_name_normalizer` and `model_name_rules`. It rejects unknown keys and
invalid values before any receiver work starts; other plugins' namespaces remain opaque.
A receiver setting left at the engine's tracing top level is rejected, not silently ignored.
Defaults and environment precedence are unchanged. A second backend need not declare any
Langfuse fields.

A launcher can also supply settings through these variables:

| Variable | Meaning |
|---|---|
| `LANGFUSE_TRACING_ENABLED` | the one switch: trace this run even without a tracing block |
| `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_EXTRA_HEADERS`, `LANGFUSE_PROJECT` | the receiver, its credentials (read through the engine's `SecretManager`) and the project the keys must open |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | the standard OpenTelemetry receiver variables, when a launcher owns the receiver |
| `TOLOKAFORGE_TRACING_TAGS`, `TOLOKAFORGE_TRACING_EXPECT_PROJECT`, `TOLOKAFORGE_TRACING_SESSION_ID`, `TOLOKAFORGE_TRACING_LABEL` | the launcher's tags, project expectation, session and label |
| `TOLOKAFORGE_TRACING_PROFILE`, `TOLOKAFORGE_TRACING_METADATA`, `LANGFUSE_ENVIRONMENT` | the deployment profile (TOML, validated at run start), the per-run metadata, the native environment override |

The engine resolves `TOLOKAFORGE_TRACING_RUN_ID` / `TOLOKAFORGE_TRACING_RUN_TAG` itself and hands
the identity over. A deployment's values (its tags, project names, environment rule, model-name
rules) never appear in this package: they arrive in the profile and the variables above.
Validate a profile with `python -m tolokaforge_langfuse.profile <file>`.

## The vocabulary and the profile

`vocabulary.py` is the default trace vocabulary, shared with the offline uploader (it imports this
module): the tag prefixes and their producer / caller split, the closed value lists the engine's
output format defines (`run_kind`, `scope`), the tags derived from the bundle and the model-name
normalizer (`model_generation`, `model_tier`, `model_variant`, `model_size`, `model_stage`,
`model_snapshot`, `reasoning_mode`, `reasoning_effort`, `reasoning_budget`, `route` = the configured provider), the tag
order and the default environment rule. `profile.py` reads a deployment's schema-2 profile (the
environment rule, fixed tags, derived-tag groups, value lists, required prefixes, derivations,
metadata keys, fixed metadata, model rules) and validates a launcher's tags and metadata against
it; `vocabulary.py`, `profile.py` and `model_names.py` import no engine module, so the offline
uploader can use them next to any engine pin. Details and a full example:
[`docs/OBSERVABILITY.md`](../docs/OBSERVABILITY.md), "The trace vocabulary" and "The deployment
profile".

## Layout

| Module | What it does |
|---|---|
| `config.py` | the strict, engine-independent `options.langfuse` schema |
| `plugin.py` | the entry point: enablement, receiver, credentials, profile, project check, the observer |
| `otel.py` | the OTLP span exporter and the `TrialObserver` implementation |
| `projection.py` | the default projection of a persisted trial bundle (the connector's `mapping.py` is the reference) |
| `gradings.py` | the grading observation, its judge transcript and scores |
| `otlp_spans.py` | the projection's ingestion bodies as OTLP spans (the write-once layout of a v4 receiver); engine-free, imported by the offline connector too |
| `media.py` | the ingestion and media REST calls, the receiver-family probe, the budget and the breaker |
| `attachments.py` | attachment manifest v2 and the data-safety scan |
| `vocabulary.py` | the default trace vocabulary: prefixes, derived tags, the environment rule (shared with the offline uploader) |
| `profile.py` | the deployment profile (schema 2; schema 1 still loads) and the launcher-input check |
| `model_names.py` | model identity as configuration (raw, or `toloka-model-name-normalizer`) |

## Tests

`tests/unit/` runs with the engine's suite (`uv run pytest tolokaforge_langfuse/tests`). The
golden parity test (`parity_bundle.py`, `parity_golden.json`) is committed byte-identically in the
connector's repository too; regenerate the golden from the connector, never by hand. The span
golden (`parity_span_golden.json`) is the same event golden run through `otlp_spans`, so both
producers derive it from one input and must agree byte for byte.
