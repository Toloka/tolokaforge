# 0033. External harness registry — operator-overridable YAML for coding-CLI parity knobs

- **Status:** Accepted
- **Date:** 2026-08-13
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Related:** [ADR-0002](0002-external-model-registry.md) — the same shape for
  the model preset registry.
- **Extended by:** [ADR-0036](0036-tolokaforge-coding-harnesses-split.md) — the
  registry lives in the `tolokaforge_coding_harnesses` package rather than inside
  the terminal-bench adapter. Everything this ADR decided — the `HarnessSpec`
  field list, load-time validation, the operator overlay, the `provider_env`
  union — is unchanged; only the address is.

## Context and Problem Statement

The terminal-bench adapter's harness-mode drives an out-of-tree coding-CLI
(Claude Code, Codex, Gemini CLI) inside the task container. A benchmark
reward is only comparable to the same CLI's out-of-tree host if the two
invocations agree on a small set of parity knobs — reasoning-mode flags,
instruction path (argv vs stdin), sub-agent model routing (env quartet
vs `--model`), root-user override (`IS_SANDBOX=1`), telemetry (`_DISABLE_NONESSENTIAL_TRAFFIC=1`),
and the ancillary on-disk state some CLIs need (codex reads
`openai_base_url` from `$CODEX_HOME/config.toml`, not the env var). Six
mechanical fixes accumulated in one afternoon just to keep three CLIs
functional on OpenRouter. Every fix left an accumulating footprint of
per-harness knobs in three separate structures:

- `HarnessSpec` (frozen dataclass) — CLI argv, install package, version,
  optional shell preamble.
- A module-level `frozenset` of harness names whose model catalog wants
  bare names, alongside a tuple of vendor-namespace prefixes to strip.
- An out-of-tree tolokaforge tooling package maintains a parallel
  per-harness envelope, mapping every harness to the `${secret:…}` and URL
  pairs its CLI needs to reach OpenRouter — reproducing per-harness
  knowledge outside this repo.

Adding a fourth harness (Grok Build, OpenCode, Kimi Code CLI) or fixing a
per-CLI routing bug touched all three structures. Two of them were Python
constants; one was out of tree. The knowledge was one thing distributed
across three shapes.

The same forces surfaced in [ADR-0002](0002-external-model-registry.md)
for the LLM preset registry a year earlier: per-entry knobs in engine
Python, a data-only change nevertheless requiring an engine release, and
external contributions gated on a release cycle. That ADR's fix — a
strict Pydantic capabilities model built from a shipped YAML plus an
operator overlay — became the pattern this ADR mirrors for harness
knobs.

The harness registry closes here as data on a single Pydantic model
loaded from a shipped YAML, with an operator-pointed overlay YAML
following the same overlay shape as ADR-0002 for the model registry.

## Decision Drivers

- **One address per harness.** "How do I add or fix a harness" must have
  one file to open — not three, not two, not one-per-repo.
- **Pydantic + `extra="forbid"` + snapshot** ([ADR-0011](0011-seam-and-declaration-conventions.md)
  Pattern B). HarnessSpec's `version` and `argv_prefix` reach the trial
  artifact via `agent_harness_version` / `agent_harness_command`; a silent
  field addition would ship a new metadata key nothing accepts.
- **Ship pure-data harness additions without an adapter release.** The
  same driver ADR-0002 called out: eval loops surface CLI-specific
  behaviours that need a policy shift, not a new class.
- **Backward-compatible defaults.** A run-config that names only
  `agent_harness` today must still work — the shipped `HarnessSpec.provider_env`
  becomes its `agent_provider_env` default; an explicit `agent_provider_env`
  merges over the top per key.
- **No thread-hostile mutation.** An operator overlay must not mutate the
  module-level registry — two adapters in one process would race each
  other's `HARNESSES`. Each adapter carries its own resolved mapping.
- **Loud-fail per AGENTS.md rule 1.** A misspelled overlay field, an
  unknown harness key, or an invalid ${secret:…} reference is refused at
  adapter construction with a message naming the file and the offending
  key.

## Considered Options

1. **Status quo** — every harness addition or fix is an adapter release
   plus a release of the out-of-tree tooling package. Rejected on the same
   grounds ADR-0002 rejected it: the release cadence gates work the release
   doesn't need to change.

2. **Pydantic HarnessSpec + shipped YAML + operator overlay YAML.**
   `data/harnesses.yaml` inside the adapter package carries the shipped
   registry; a run-config path (`harness_adapter.params.harness_presets_file`)
   points at an overlay YAML the adapter merges on top. Pattern mirrors
   ADR-0002 verbatim for the LLM registry.

3. **Entry-point plugin discovery for harness bundles.** A
   `tolokaforge_adapter_terminal_bench.harness_registries` entry-point
   group. Operators install a `pip` package that ships a YAML file. Same
   distribution mechanism ADR-0002 deferred as a follow-up. Deferred here
   for want of evidence of cross-project reuse; adopted in
   [ADR-0034](0034-external-harness-plugin-discovery.md) once the Arena
   expansion supplied it.

4. **Sidecar Python module for new harness classes.** For a CLI whose
   invocation shape doesn't fit any HarnessSpec field (e.g. a CLI that
   needs a stateful auth negotiation before the first message). Deferred
   — the accumulated matrix data doesn't yet surface a CLI needing this.

## Decision

Adopt **Option 2 — the shipped YAML + operator overlay pattern.**

### `HarnessSpec` shape

- **Pydantic `BaseModel` with `model_config = ConfigDict(extra="forbid", frozen=True)`**.
  Adding a field requires an ADR update and a snapshot regen; mutation on
  an instance is refused at runtime.
- Fields (as of this ADR):
  - `install_method: Literal["npm", "pip", "curl-bash", "binary"]`,
    `install_source: str`, `version: str` — installation. The method
    dispatches inside `install-harness.sh` and constrains the source
    (package name vs. download URL), validated at load.
  - `argv_prefix`, `argv_suffix`, `flags_pre_permission`, `model_flag`,
    `model_flag_style`, `instruction_channel`, `env_model_vars` — argv
    assembly.
  - `config_files: dict[str, str]` — container path to Jinja template for
    CLIs configured by file. Rendered per trial against a closed variable
    set (`model`, `provider`, `base_url`, `api_key_env`); an undeclared
    name is refused at load. A path is absolute, a `${HOME}` /
    `${CONFIG_HOME}` construct the run's `PathResolver` answers (shipped
    default `LinuxRootResolver` → `/root`, `/root/.config`), or any other
    `$VAR` reference, left verbatim for the container's own shell.
  - `container_env: dict[str, str]` — compose environment lines the
    agent service always carries.
  - `strip_vendor_namespace: bool` — whether to strip a `vendor/` prefix
    from the model name before handing it to the CLI.
  - `strip_openrouter_prefix: bool = True` — whether the `openrouter/`
    route marker on the model name is stripped before it reaches the CLI.
    Default preserves the vendor-CLI behavior (the CLI reads `*_BASE_URL`
    for OpenRouter and would 401 on its native handler otherwise).
    `False` on opencode, whose config template defines a provider literally
    named `openrouter` and expects the caller to route
    `openrouter/<vendor>/<model>` to it.
  - `request_middleware: RequestMiddleware | None` — declares a stdlib
    HTTP proxy (`middleware_proxy.py`) that lands inside the trial image
    and rewrites the CLI's provider requests before they leave the
    container. The record names an env-var whose URL value gets redirected
    to `http://127.0.0.1:<port>`, plus body deep-merge / header-inject /
    URL-path-filter fields the proxy applies before forwarding upstream.
    Cannot coexist with `config_files` (config templates render at
    Python-assembly time from the upstream URL; the middleware rewrite
    only reaches env-driven routing). Motivating case: kimi-code injects
    `provider.only=["moonshotai"]` on every `/chat/completions` body to
    force Moonshot AI first-party routing on OpenRouter.
  - `provider_env: dict[str, str]` — the shipped default `agent_provider_env`
    envelope for this harness (URLs, `${secret:…}` refs).
- Canonical snapshot at `tests/canonical/snapshots/tbench_echo_hello_harness/harness_spec.json`
  pins the JSON wire shape (Pattern B invariant).

### Registry loading

- `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`
  is the shipped source of truth for the three current entries. The
  hardcoded `HARNESSES = {...}` block is replaced by a loader:

  ```python
  HARNESSES: dict[str, HarnessSpec] = load_harness_registry(SHIPPED_REGISTRY_FILE)
  ```

- `load_harness_registry(path)` reads the YAML, validates each entry via
  `HarnessSpec.model_validate(entry)`, and returns `dict[str, HarnessSpec]`.
  An invalid YAML file, a missing required field, or a `Pydantic ValidationError`
  is wrapped with a message naming the file path and the offending harness
  key. Warnings during load are also fatal.

### Operator overlay

- `TerminalBenchAdapter` accepts a new param
  `harness_presets_file: str | None = None` under
  `evaluation.harness_adapter.params`. When set, points at a YAML of the
  same top-level shape as `data/harnesses.yaml`.
- **Merge semantics: whole-entry replacement.** A harness the overlay
  declares replaces the shipped spec completely (Pydantic
  `HarnessSpec.model_validate` runs on the overlay entry alone; no
  partial-field merge). A harness the overlay does not name is left
  untouched. Partial-field merging would let an overlay inherit a shipped
  default it never meant to keep — a pinned version, a mandatory
  permission-bypass flag, an `env_model_vars` quartet — and produce an
  invocation neither side declared. The overlay may also add a new
  harness the adapter does not ship; `install-harness.sh` installs
  whatever install method, source and version it names.
- **Per-adapter registry, not global mutation.** `HARNESSES` (the shipped
  default) stays module-level and is what module-level helpers
  (`validate_harness`, `harness_model`, `harness_command`,
  `materialise_task_environment`) fall back to. The adapter constructs
  its own resolved `dict[str, HarnessSpec]` and threads it through every
  call site that needs the overlaid spec, so two adapters in one process
  cannot see each other's overlay.
- **The resolved spec is part of the staging digest.** Without it, two
  adapters differing only by an overlaid spec would share a staging dir
  and overwrite each other's Dockerfile. `ACCEPTED_HARNESSES` becomes
  `accepted_harnesses(registry)`, since with an overlay the accepted set
  is per-adapter.
- Loud-fail: a missing file, malformed YAML, or an invalid entry raises
  at adapter construction naming the file *and* the failing harness key.

### `provider_env` union with run-config `agent_provider_env`

- `HarnessSpec.provider_env` is the **shipped default** the CLI needs to
  reach its provider. Populated once per harness in
  `data/harnesses.yaml`:
  - claude-code → `ANTHROPIC_API_KEY=${secret:OPENROUTER_API_KEY}` +
    `ANTHROPIC_BASE_URL=https://openrouter.ai/api`.
  - codex → `OPENAI_API_KEY=${secret:OPENROUTER_API_KEY}` +
    `OPENAI_BASE_URL=https://openrouter.ai/api/v1`.
  - gemini-cli → `GOOGLE_API_KEY=${secret:OPENROUTER_API_KEY}`.
- **Union at construction time**: the effective envelope is
  `HarnessSpec.provider_env | run_config.agent_provider_env`, run-config
  winning per key. A run-config naming only `agent_harness: claude-code`
  reaches the provider on the shipped envelope; one that points the CLI
  at a different endpoint keeps the credential rather than restating it.
- `PROVIDER_ENV_KEYS` allow-list validation and the newline / `$` refusal
  run on the **effective** envelope. Unresolvable `${secret:…}` refs
  name the harness that shipped them (not an `agent_provider_env` block
  the operator never wrote).
- `engine-loop` has no spec, so it forwards nothing and constructs no
  `SecretManager`. Unchanged.

## Consequences

### Positive

- One YAML file to edit to add a new harness. No adapter release for a
  data-only change.
- HarnessSpec's snapshot pins the wire shape; a silent field addition
  fails CI.
- Existing run-configs continue to work identically. A run-config that
  used to declare its own `agent_provider_env` still overrides the
  harness default — which is what it was implicitly doing before.
- The out-of-tree tooling package's parallel mapping becomes deletable —
  the per-harness envelope now lives in one place.
- Two adapters in one process can carry independent overlays without
  race.

### Negative / Trade-offs

- The out-of-tree mapping isn't deleted by this ADR — a coordinated
  release lag remains until that package picks up the tolokaforge version
  carrying `HarnessSpec.provider_env`.
- Overlay's whole-entry replacement means an operator wanting to change
  one field (e.g. bump `version`) copies the whole entry. The alternative
  (field-wise merge) has worse failure modes; this is the deliberate
  trade-off. If operators complain, a subsequent ADR can add an explicit
  `inherit_from: <name>` field to the overlay entry.
- Entry-point plugin discovery for harness bundles (Option 3) lands
  separately in [ADR-0034](0034-external-harness-plugin-discovery.md), so a
  `pip install` can change which spec a harness name resolves to. The
  overlay stays the highest-precedence layer, and
  `disable_harness_plugins` pins the registry to what the adapter ships.

### Follow-ups

- **Out-of-tree tooling migration**: delete that package's parallel
  per-harness envelope once it picks up the tolokaforge release carrying
  `HarnessSpec.provider_env`.
- **Entry-point plugin discovery** for harness bundles — landed as
  [ADR-0034](0034-external-harness-plugin-discovery.md). A contributor
  publishes a package declaring
  `tolokaforge_adapter_terminal_bench.harness_registries`; the adapter unions
  every installed bundle above the shipped registry and below the operator
  overlay, refusing two plugins that claim one harness name.
- **Sidecar Python module for new harness classes** — file when a CLI
  surfaces that doesn't fit any HarnessSpec field.
- **Task-pack skills bundle** — landed. A task pack declares
  `harness_skills_dir: <task-relative path>` in its `task.yaml`, and
  `HarnessSpec.skills_dir_target` names where a harness wants such a
  bundle — absolute, or rooted at a `${HOME}` / `${CONFIG_HOME}` construct
  the run's `PathResolver` answers (`${HOME}/.claude/skills/` for
  claude-code; unset means the harness installs none). *How* the bundle
  travels is the run's `SkillDelivery`; the shipped
  `ImageLayerSkillDelivery` copies the directory into the image layer, and
  `TaskDescription.metadata["harness_skills_bundle_sha"]` records its
  content hash — the reproducible, auditable replacement for the
  operator-environment contamination the out-of-tree host smuggles. The
  declared path is refused unless it resolves inside the task pack, symlinks
  included.
- **Skills targets for the non-Anthropic harnesses** — only claude-code
  declares a `skills_dir_target`. Each other CLI needs its own skills
  path established against that CLI's documented discovery order before
  it can claim one; until then those harnesses install no skills and say
  so with a warning.

## Links

- Related ADRs:
  - [ADR-0002](0002-external-model-registry.md) — the same pattern for
    the model registry.
  - [ADR-0036](0036-tolokaforge-coding-harnesses-split.md) — where this
    registry lives now, and why the move changed nothing it decided.
  - [ADR-0011](0011-seam-and-declaration-conventions.md) Pattern B —
    HarnessSpec is a data declaration crossing the adapter → artifact
    boundary.
- Related code:
  - `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/_registry.py`
    — `HarnessSpec`, `load_harness_registry`, `SHIPPED_REGISTRY_FILE`.
  - `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`
    — the shipped registry.
  - `external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py`
    — `harness_presets_file` param, overlay wiring, `provider_env` union.
  - `external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/task_parser.py`
    — `harness_skills_dir` parsing and its containment check.
