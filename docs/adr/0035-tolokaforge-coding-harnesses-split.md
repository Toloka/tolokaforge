# 0035. Coding-harness code as a top-level workspace package — `tolokaforge_coding_harnesses`

- **Status:** Accepted ([#1233](https://github.com/Toloka/tolokaforge/issues/1233))
- **Date:** 2026-08-18
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Extends:** [ADR-0033](0033-external-harness-registry.md) and
  [ADR-0034](0034-external-harness-plugin-discovery.md) — this ADR moves the code
  those two decided. The `HarnessSpec` field list, the three-layer merge
  precedence, the entry-point group and the plugin contract are unchanged; only
  the address is new.
- **Related:** [ADR-0030](0030-tolokaforge-models-split.md) — the structural
  template. Same top-level-package shape, same repo-split forward-compat, but no
  PyPI release axis (see § Considered Options).

## Context and Problem Statement

The coding-harness surface — the `HarnessSpec` registry, six vendor-CLI
installers (claude-code, codex, gemini-cli, grok-build, kimi-code, opencode),
the middleware proxy, and two runtime scripts — shipped inside the terminal-bench
adapter, at
`external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/harness/`
with its data at `.../tolokaforge_adapter_terminal_bench/data/{harnesses,registry_meta}.yaml`.
This ADR is the record of that move, and the only document that names the
pre-move location.

Three forces made the address wrong:

- **Harness edits rode an adapter release.** [ADR-0033](0033-external-harness-registry.md)
  made the registry *data* so that adding a harness or bumping a pinned CLI
  version is a YAML edit — but the YAML shipped in the adapter's wheel, so the
  edit still travelled on the adapter's cadence. The same argument
  [ADR-0030 § Context](0030-tolokaforge-models-split.md#context-and-problem-statement)
  makes for model data applies unchanged: what agent ran is the largest variable
  in a coding benchmark, and it should not be a question about which adapter
  version was installed.
- **A second consumer had no address.** Three further adapters are candidate
  consumers of the registry ([#1230](https://github.com/Toloka/tolokaforge/issues/1230),
  [#1231](https://github.com/Toloka/tolokaforge/issues/1231)). Reaching harness
  data meant importing the *terminal-bench* adapter — a sibling plug-in — which
  is a dependency edge no adapter should have on another.
- **The subtree was already a leaf.** Every import inside it was intra-package
  relative or stdlib / third-party, with exactly one outward engine import: the
  fail-loud entry-point scan `tolokaforge.core.plugin_registry.discover_entry_points`.
  Nothing under `tolokaforge/` imported the harness surface in the other
  direction. The move was therefore a relocation, not a decoupling project.

## Decision Drivers

- **Any runtime can read the registry without inheriting an engine version pin.**
  A harness trial is driven by whoever runs the container; the facts about *what
  the CLI is and how it is invoked* should be readable by an adapter, a second
  adapter, or a future runner-side consumer, none of which should have to agree
  on an engine version to do it.
- **No adapter imports another adapter.** Shared harness data is shared
  infrastructure, not one plug-in's surface.
- **Verbatim move.** Behaviour bit-identical, with exactly one documented
  exception (the duplicate-registration class, below). A hoist that also
  refactors cannot be reviewed as either.
- **Installed third-party plug-ins keep resolving.** [ADR-0034](0034-external-harness-plugin-discovery.md)
  published an entry-point group as the external contributor's contract; a
  structural move is the wrong PR to migrate it.
- **Forward-compat toward a repo split**, per ADR-0030: the tree is laid out so
  that `git filter-repo --subdirectory-filter tolokaforge_coding_harnesses`
  yields a standalone project.

## Considered Options

1. **Status quo — harness code stays inside the terminal-bench adapter.**
   Rejected: this is the problem.

2. **Move it into the engine (`tolokaforge/`).** Rejected. The engine core never
   learns harness names ([ADR-0033 § Context](0033-external-harness-registry.md#context-and-problem-statement),
   AGENTS.md Core Rule 2) — it receives a fully-assembled command on
   `TaskDescription.metadata` and runs it. Nothing under `tolokaforge/` imports
   the surface, so this would add a dependency the engine does not have and bake
   harness code into the engine wheel and the runner image.

3. **A third PyPI distribution with its own release axis** — commitizen
   configuration, publish workflow, `CHANGELOG.md`, `RELEASING.md` section, the
   full ADR-0030 treatment. Rejected *for now*: the driver is the wrong address,
   not the release cadence, and an unpublished-but-releasable package is the
   cheaper first step. `version = "0.1.0"` and the absence of release plumbing
   are what mark the package workspace-only. Publishing it is a follow-up, and a
   prerequisite for the next adapter release (§ Release ordering).

4. **A top-level workspace member with no release plumbing.** Selected.

## Decision

Adopt **Option 4.**

### The package

```
public-tolokaforge/
└── tolokaforge_coding_harnesses/
    ├── pyproject.toml                     # hatchling; version 0.1.0; no commitizen
    ├── src/tolokaforge_coding_harnesses/
    │   ├── __init__.py                    # the public surface, re-exported flat
    │   ├── _registry.py                   # HarnessSpec, loading, discovery, harness_command
    │   ├── fingerprint.py                 # HarnessFingerprint over resolved registry content
    │   ├── path_resolvers.py              # shipped PathResolver implementations
    │   ├── protocols.py                   # PathResolver / SkillDelivery / SkillsBundle contracts
    │   ├── middleware_proxy.py            # stdlib HTTP forwarder shipped into trial images
    │   ├── install-harness.sh             # the four-way install dispatcher
    │   ├── testing.py                     # fake entry points / distributions for plug-in tests
    │   └── data/{harnesses,registry_meta}.yaml
    └── tests/unit/                        # the suite that locks all of the above
```

Callers import from the package root (`from tolokaforge_coding_harnesses import
harness_command`); `_registry.py` is private, and the names it defines are
re-exported. `install-harness.sh`, `middleware_proxy.py` and both YAML files are
inside the wheel by hatchling's include-the-package-directory convention, which
a load-time package-sibling assertion verifies rather than assumes.

### The invariant is "no `tolokaforge.*` import", not "no dependencies"

`tolokaforge_models` can declare `dependencies = []` because it is data plus
light policy. This package cannot and must not copy that stanza: the registry
loader reads YAML, the `config_files` renderer is Jinja2, and `HarnessSpec` is
Pydantic, so `pyyaml`, `jinja2` and `pydantic` are declared dependencies.

What makes the package consumable by any context is that it never imports
`tolokaforge.*` — so it carries no engine-version coupling and no
distribution-name assumption (the engine Python package is provided by two
different distributions depending on install context, per
[ADR-0025](0025-runner-wheel-split.md)). That invariant is locked by a test that
imports the package in a **fresh interpreter** and asserts no `tolokaforge*`
module landed in `sys.modules`; in-process it would pass by accident, because
the surrounding suite has already imported the engine.

### Three behaviour-preserving edits

The move is verbatim except for exactly three changes, each forced by the new
location:

1. **Entry-point discovery is package-local.** The one outward engine import is
   replaced by a scan over `importlib.metadata` of the same shape: enumerate
   names and distributions without loading any target, refuse a duplicate name
   before writing the cache, cache per group. `entry_points` is read off the
   module **at call time** rather than bound with a from-import, because that
   attribute is the seam a test replaces to make a fabricated bundle the
   installed set — a module-scope binding would leave every plug-in test
   silently exercising whatever is really installed. A package-local
   `_clear_discovery_cache()` gives suites that inject different installed sets
   the isolation the engine primitive gave them.
2. **Duplicate registration raises a package-local error.** See below.
3. **The two data-path constants resolve `data/` as a sibling of the module**
   rather than of the package directory. `INSTALL_SCRIPT` and
   `MIDDLEWARE_PROXY_SCRIPT` were already module-relative and are untouched.

### Duplicate registration raises a package-local `DuplicateRegistrationError`

Package-local discovery cannot raise the engine's
`tolokaforge.core.plugin_registry.DuplicateRegistrationError` without importing
the engine and losing the invariant. `tolokaforge_coding_harnesses` therefore
defines its own class of **the same name**, subclassing `ValueError`, with the
same message shape naming both providing distributions.

What ADR-0034 documented as the observable outcome — the name, and a fail-loud
message an operator can act on — is preserved; only class identity changes.
`ValueError` as the base means a caller already refusing malformed registry input
keeps catching this too.

**Two classes of one name now exist in the tree**, deliberately. The engine's
class is unchanged and still governs the engine's own entry-point seams
(`docs/RUNTIME_BACKENDS.md`, `docs/STANDALONE_RUNNER.md`,
`docs/ADAPTER_ARCHITECTURE.md`, `tolokaforge/adapters/__init__.py` all describe
that one, accurately, in their own context). Harness-registry discovery raises
the package-local one. Code that catches the engine class specifically around
*harness* discovery must switch; a broad `except ValueError` needs no edit.

### The entry-point group name is a retained compatibility artefact

`HARNESS_REGISTRY_ENTRY_POINT_GROUP` remains the string
`"tolokaforge_adapter_terminal_bench.harness_registries"`. It is now a
compatibility artefact rather than a description of ownership: the group is
declared and consumed by `tolokaforge_coding_harnesses`, not by the adapter it
is named after. Renaming it would break every out-of-tree bundle registered
under the old group, and a structural hoist is the wrong PR to carry that
migration; a rename — with a transition that reads both groups and warns on the
old one — is deferred to its own change.

### The Python import path moves with no shim

`tolokaforge_adapter_terminal_bench.harness` (and its `.fingerprint`,
`.protocols`, `.middleware_proxy` submodules) no longer exist. Nothing is
re-exported under the old path, deliberately: a shim would keep alive exactly
the address this ADR retires, and the failure mode of its absence is a loud
`ModuleNotFoundError` at import rather than a silent divergence. The adapter is
consumed through the `tolokaforge.adapters` entry point and no documented recipe
imported the harness module directly. `CHANGELOG.md` carries the migration under
*Changed*.

### No runner-subset `force-include`

The engine's runner-subset wheel force-includes `tolokaforge_models/`'s
`pyproject.toml` and `src/` because the runner Dockerfile builds that wheel
in-container and `builder._runner_definition()` needs a mapped context entry.
There is no equivalent runner-side consumer of the harness surface — nothing
under `tolokaforge/` imports it — so force-include entries would ship dead bytes
and lock nothing. If a runner-side consumer ever lands, that change adds the
force-include, the context entry and the Dockerfile `COPY` together.

### Release ordering is load-bearing

The terminal-bench adapter declares `tolokaforge-coding-harnesses` in its
`[project].dependencies` — it imports the package, so the dependency is real —
and that distribution is **not on PyPI**. Until it is published, an
`adapter-terminal-bench-v*` tag would produce a wheel whose dependency no
consumer can resolve. The engine wheel is unaffected: it does not declare the
package, because it does not import it.

Same hazard, same shape as
[ADR-0030 § What "success" looks like](0030-tolokaforge-models-split.md#what-success-looks-like)'s
publish-ordering clause: publish `tolokaforge-coding-harnesses` first, or keep
both unpublished. The follow-up below is a prerequisite for the next adapter
release, not a nice-to-have.

## Consequences

### Positive

- A harness edit — new entry, bumped CLI pin, new install method, a middleware
  body override — touches one package and no adapter source tree.
- A second consumer imports `tolokaforge_coding_harnesses` directly. #1230 and
  #1231 stop needing a dependency on the terminal-bench adapter.
- The package is readable by a context that does not install the engine at all,
  and the fresh-interpreter test keeps it that way: a stray `import tolokaforge`
  added later fails in this repo's own suite rather than in a downstream
  consumer's environment.
- A repo split is mechanical — one `git filter-repo --subdirectory-filter`, the
  ADR-0030 property, now available for the harness surface too.
- Extension by pip-installable bundle (ADR-0034) is unchanged, and the group
  string is unchanged, so no installed bundle needs an edit.

### Negative / Trade-offs

- **A published import path disappeared with no deprecation window.** Any
  out-of-tree code importing `tolokaforge_adapter_terminal_bench.harness`
  breaks at import. Judged acceptable because the failure is immediate and
  actionable and the fix is one line, but it is a real break, recorded in the
  CHANGELOG under *Changed*.
- **The entry-point group name no longer describes ownership.** A reader
  encountering `tolokaforge_adapter_terminal_bench.harness_registries` has to
  come here to learn why. The alternative was breaking installed plug-ins.
- **Two `DuplicateRegistrationError` classes share a name.** A reader tracing
  the name has to notice which module raised it; both this ADR and ADR-0034 say
  so explicitly.
- **The adapter cannot be released until the package is published** (§ Release
  ordering). The hoist created a real, if temporary, release-ordering
  constraint.
- **No independent release cadence yet.** The package is workspace-only, so
  "harness edits no longer ride an adapter release" is true inside the monorepo
  and not yet true for a PyPI consumer.

### Follow-ups

- **Publish `tolokaforge-coding-harnesses` to PyPI** — commitizen axis, publish
  workflow, `CHANGELOG.md`, and a `docs/RELEASING.md` section, mirroring
  `tolokaforge-models`. Prerequisite for the next `adapter-terminal-bench-v*`
  tag, and the step that turns the independent-cadence claim into a fact for
  consumers.
- **Rename the entry-point group** to a name describing the package that owns
  it, behind a transition that reads both groups and warns on the old one.
- **Split `_registry.py`** into spec / registry / command modules. Deferred out
  of the hoist so that the move stayed reviewable as a move; the file is 1100
  lines and three concerns.
- **A runner-side consumer**, if one materialises, lands the force-include, the
  `_runner_definition()` context entry and the Dockerfile `COPY` together.

## Links

- Related ADRs:
  - [ADR-0033](0033-external-harness-registry.md) — the registry data and the
    operator overlay; unchanged in shape and semantics by this move.
  - [ADR-0034](0034-external-harness-plugin-discovery.md) — the entry-point
    group and the plugin contract, both retained; its duplicate-registration
    clause is refined here.
  - [ADR-0030](0030-tolokaforge-models-split.md) — the top-level-package shape
    this one mirrors, and the release-ordering hazard it names.
  - [ADR-0025](0025-runner-wheel-split.md) — why "no engine import" and not "no
    dependency on the engine distribution" is the testable invariant.
  - [ADR-0011](0011-seam-and-declaration-conventions.md) Pattern B — the
    `HarnessSpec` declaration convention, carried over unchanged.
- Related code:
  - `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/_registry.py`
    — `HarnessSpec`, `load_harness_registry`, `resolve_effective_registry`,
    `harness_command`, `DuplicateRegistrationError`,
    `HARNESS_REGISTRY_ENTRY_POINT_GROUP`.
  - `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`
    — the shipped registry.
  - `tolokaforge_coding_harnesses/tests/unit/test_package_boundary.py` — the
    fresh-interpreter no-engine-import lock and the packaged-data assertions.
  - `external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py`
    — the consumer: effective-registry wiring, `harness_presets_file`,
    `disable_harness_plugins`, fingerprint recording.
