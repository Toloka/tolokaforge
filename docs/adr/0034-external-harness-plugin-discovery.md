# 0034. External harness plugin discovery — pip-installable harness bundles

- **Status:** Accepted
- **Date:** 2026-08-13
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Extends:** [ADR-0033](0033-external-harness-registry.md) — realises its
  **Option 3**, the entry-point plugin discovery that ADR deferred. The shipped
  YAML and the operator overlay it decided are unchanged; this ADR adds one
  layer between them.
- **Related:** [ADR-0002](0002-external-model-registry.md) — the ancestor of the
  same shape for the model preset registry, which deferred its own Option 3 on
  the same grounds.

## Context and Problem Statement

[ADR-0033](0033-external-harness-registry.md) closed the harness registry as
data: `data/harnesses.yaml` inside the adapter wheel, plus an
operator-pointed overlay named by
`evaluation.harness_adapter.params.harness_presets_file`. That covers two of
the three people who want to add a harness, and leaves out the third:

- The **maintainer** adds an entry to the shipped YAML. Data-only change, but
  still an adapter release, and it commits Toloka to carrying that harness.
- The **operator** points `harness_presets_file` at a YAML on the machine
  running the eval. No release needed, but the file is a laptop artefact: not
  installable, not versioned with anything, not discoverable by anyone else
  on the team, and re-derived per machine.
- The **external contributor** has neither address. Their harness is either
  upstreamed into a repo they do not own, or it is a YAML they mail around.

ADR-0033 deferred entry-point discovery for want of "evidence of cross-project
reuse". The Arena expansion supplied it: three CLIs (Kimi Code, OpenCode, Grok
Build) landed in the shipped YAML in one week, each needed by a matrix run
rather than by tolokaforge itself, and each carrying knobs — an install
method, a config-file template, a provider envelope — whose owner is the CLI's
community, not this repo. A fourth wave will not fit in the shipped file, and
should not: the shipped registry's job is the harnesses this repo will keep
working, not every harness anyone runs.

The missing address is a distribution mechanism: a way for a harness bundle to
be a thing you `pip install`, that says who published it, and that the adapter
finds without being told.

## Decision Drivers

- **An external contributor can ship a harness bundle without an adapter
  release** — and without commit rights on this repo. The driver ADR-0033's
  Option 3 named and deferred.
- Everything ADR-0033 already decided: **one address per harness**, **Pydantic
  + `extra="forbid"` + snapshot** ([ADR-0011](0011-seam-and-declaration-conventions.md)
  Pattern B), **no thread-hostile mutation** of the module-level registry, and
  **loud-fail per AGENTS.md rule 1**.
- **Attribution, not injection.** A harness that arrives from outside the wheel
  must say so. What agent ran is the largest variable in a coding benchmark,
  so "where did this spec come from" has to be answerable from the run's logs.
- **Reproducibility on demand.** A run whose result is an audit artefact must
  be able to pin the registry to what the adapter ships, independent of what
  else is installed in the same environment.
- **Align with the engine's existing plugin surface.** The repo already
  discovers adapters, runtime backends, trial graders, conductors, readiness
  probes and turn policies through `importlib.metadata` entry points and one
  fail-loud primitive (`tolokaforge.core.plugin_registry.discover_entry_points`).
  A seventh mechanism would be a seventh thing to learn.

## Considered Options

1. **Status quo — the manual overlay only.** The external contributor keeps
   mailing a YAML around, or upstreams into a repo they do not own. Rejected:
   this is exactly the gap the Arena expansion surfaced, and the overlay's
   laptop-locality is the property that makes it unshippable.

2. **Entry-point discovery via a
   `tolokaforge_adapter_terminal_bench.harness_registries` group.** A
   contributor publishes a package declaring the group; the adapter enumerates
   installed declarations at construction and unions their bundles under the
   shipped defaults and beneath the operator overlay. This ADR's proposal.

3. **An environment variable naming a directory of YAMLs**
   (`TBENCH_HARNESS_REGISTRY_DIR`). Cheapest to build — a `glob` and a loop.
   Rejected: it is the overlay's laptop-locality with a wider blast radius. A
   directory has no publisher, no version, and no uninstall; `pip list` cannot
   answer what is in it; a stale file nobody remembers dropping there silently
   changes which CLI version a benchmark measures. ADR-0002 dropped a
   `TOLOKAFORGE_PRESETS_FILE` env fallback for the neighbouring reason, and
   the harness registry — which decides what agent runs — is the surface where
   ambient configuration is least acceptable.

## Decision

Adopt **Option 2 — the entry-point group.**

### The group and the plugin contract

```toml
[project.entry-points."tolokaforge_adapter_terminal_bench.harness_registries"]
my_org = "my_org.tolokaforge_harnesses"
```

- The group name lives in exactly one place,
  `HARNESS_REGISTRY_ENTRY_POINT_GROUP` in
  `tolokaforge_adapter_terminal_bench.harness`, and is exported.
- It is **adapter-namespaced**, not `tolokaforge.*`: the harness registry is
  the terminal-bench adapter's surface, and the engine core never learns these
  names ([ADR-0033](0033-external-harness-registry.md) § Context). A
  `tolokaforge.harness_registries` group would imply the engine consumes it.
- The entry point's **value is the plugin's Python package**; the package
  ships its registry beside its `__init__.py` as `harnesses.yaml`, read
  through `importlib.resources`. The file name is a convention
  (`PLUGIN_REGISTRY_RESOURCE`), not a second declaration: a
  `HARNESS_REGISTRY_FILE` module attribute would be a second address for one
  fact, and could disagree with what the wheel actually contains — the failure
  ADR-0033's "one address per harness" driver exists to prevent. A contributor
  copying the shipped registry's own file name is the whole contract.
- The bundle's shape is `data/harnesses.yaml`'s shape, loaded by the same
  `load_harness_registry`. A plugin's typo is refused with the message an
  operator overlay's would get, naming the file and the harness key.

### Merge precedence

`resolve_effective_registry` composes the three sources, lowest to highest,
**whole-entry replacement at every transition**:

```
shipped data/harnesses.yaml
  ← plugin bundles (entry-point discovery)   — plugin wins, warning logged
  ← operator overlay (harness_presets_file)  — overlay wins, silently
```

Whole-entry replacement, never field-wise, for ADR-0033's reason: a merge
would let a bundle silently inherit a default it never meant to keep — a
pinned CLI version, a mandatory permission flag, an `env_model_vars` quartet —
and produce an invocation no layer declared.

A plugin shadowing a shipped harness logs a **warning** naming the shadowed
keys: the operator did not ask for it by name, so the substitution has to be
visible. The overlay shadowing anything logs nothing — naming the file *is*
the intent.

### Plugin-vs-plugin collision is a load-time error

Two installed plugins declaring the same harness name raise `ValueError`
naming **both distributions** and the harness. There is no safe pick: the two
bundles disagree about what that name installs and how it is invoked, so
install order must not decide which agent a benchmark measures. This mirrors
`DuplicateRegistrationError`, which the engine's plugin registry raises for
the same ambiguity one level up (two distributions claiming one entry-point
*name*) — and which this adapter inherits for free by scanning through
`discover_entry_points`.

### Discovery is opt-out

- Default **on**. Installing the bundle is the operator's declaration of
  intent; requiring a second opt-out-of-the-opt-in flag would put the
  contributor's package back behind a per-run configuration step and defeat
  the driver.
- `disable_harness_plugins: bool = False` on the adapter params pins the
  effective registry to shipped-plus-named-overlay, for runs that must
  reproduce independently of what is installed alongside them. It **skips the
  scan**, rather than discarding its result, so a broken or colliding plugin
  in the environment cannot fail an audit run that does not use it.

### Attribution

Each contributing distribution logs at INFO with the harness keys it supplied.
No plugin, no line: the common case stays silent.

## Consequences

### Positive

- An external contributor publishes a wheel and their harness is installable,
  versioned, attributable, and removable with `pip uninstall`. `pip list`
  answers "what harnesses can this environment run" without reading source.
- The shipped registry stops being the only address for a harness anyone
  wants, so it can stay what it should be: the set this repo keeps working.
- Nothing changes when no plugin is installed. The shipped-plus-overlay
  behaviour ADR-0033 decided is bit-identical, and the existing canonical
  snapshots stay valid — a plugin bundle produces the same `HarnessSpec`
  through the same loader, so there is no new wire shape to pin.
- One discovery primitive across the repo's seven entry-point groups, one
  duplicate-registration failure mode, one thing to learn.

### Negative / Trade-offs

- **A pip install can change what agent a benchmark measures.** That is the
  feature, and it is also the risk: a plugin shadowing `codex` replaces its
  pinned version and argv. Mitigated by the warning, the INFO attribution and
  `disable_harness_plugins`, but not eliminated — an operator who installs a
  bundle is trusting its publisher exactly as they trust any dependency.
- **A plugin bundle is not part of the staging digest by publisher.** The
  *resolved spec* is (ADR-0033), so two adapters differing by an overlaid or
  plugin-supplied spec do not share a staging directory. But two plugins
  shipping byte-identical specs under one name are indistinguishable to the
  digest — correctly, since they would produce the same image and the same
  invocation.
- **Import cost.** Each declaring package is imported to resolve its resource.
  Bounded by how many bundles are installed, which is small, and paid once per
  adapter construction.
- Collision refusal is per-*harness*, not per-bundle: two plugins can coexist
  as long as their harness names are disjoint. A contributor who wants to
  ship a variant of somebody else's harness must name it differently, which is
  the honest outcome — two things that install differently are two harnesses.

### Follow-ups

- **A published example bundle.** The `pyproject.toml` snippet in the adapter
  README is the whole contract, but a real minimal package on PyPI would be a
  faster starting point for the first external contributor. File when one asks.
- **Sidecar Python module for new harness classes** — still open from
  ADR-0033. A CLI whose invocation shape fits no `HarnessSpec` field needs
  code, not data, and no entry-point group makes that a YAML.

## Links

- Related ADRs:
  - [ADR-0033](0033-external-harness-registry.md) — the shipped YAML and
    operator overlay this ADR layers between; its Option 3 is what this
    decides.
  - [ADR-0002](0002-external-model-registry.md) — the same deferral, and the
    same eventual shape, for the model preset registry.
  - [ADR-0011](0011-seam-and-declaration-conventions.md) Pattern B — a plugin
    bundle is a data declaration, validated by the same model as the shipped
    file.
- Related code:
  - `external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/harness/__init__.py`
    — `HARNESS_REGISTRY_ENTRY_POINT_GROUP`, `PLUGIN_REGISTRY_RESOURCE`,
    `discover_plugin_harness_registries`, `resolve_effective_registry`.
  - `external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py`
    — `disable_harness_plugins` param, effective-registry wiring.
  - `tolokaforge/core/plugin_registry.py` — `discover_entry_points`, the
    fail-loud scan this reuses.
- External references:
  - [Python packaging: entry points](https://packaging.python.org/en/latest/specifications/entry-points/).
