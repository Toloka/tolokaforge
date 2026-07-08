# Project-level environment manifest + per-task recipe overlays

*Design proposal · Draft for review · Not an ADR yet*

Companion artefact: [`examples/native/example-project-microservices/`](../../../examples/native/example-project-microservices/)
— a spec-only task pack that shows the proposed schema in action.
Read this document with that directory open side-by-side; the doc
describes the semantics, the example makes them concrete.

## TL;DR

- Today's task-pack model forces each task to declare its own
  `environment_manifest` — five tasks that all want the same
  postgres + backend + frontend + queue stack must copy the same
  compose file into five task directories, with no shared source of
  truth. That's the pain.
- **Proposal**: introduce a `project.yaml` file at the pack root that
  declares a shared base environment + typed inputs + service
  profiles. Individual tasks may optionally ship a `recipe.yaml`
  that overrides typed inputs, selects/drops profiles, or replaces
  specific services outright. Two-level precedence
  (`Project > Task recipe`) — no deeper.
- **Design principle** — borrowed from Kubernetes Kustomize +
  Terraform + Docker Compose profiles, deliberately not borrowed
  from GitLab CI or Ansible: **explicit typed inputs + service
  profiles, never free-form deep-merge**. Every override point is
  declared on the schema.
- **Extensibility mechanism** — service-type registry (each service
  type is a plugin: `postgres`, `redis`, `http-static`, …) so new
  service types + their reset primitives can ship as third-party
  packages via entry-points, without a schema change. Ties into the
  broader Protocol-as-entry-point direction already being explored.
- **Backward compatibility** — task packs without a `project.yaml`
  keep working unchanged. The feature is opt-in.
- **Scope of this document** — semantics, not implementation. Every
  file in the companion example is illustrative — the loader does
  not understand `project.yaml` yet. This is what colleagues review
  before implementation starts.

## 1 · The problem

Today, a TolokaForge task ships its environment declaration inside
its own directory:

```
examples/native/multi_service_postgres/dataset/tasks/multi_service/support_triage_01/
├── task.yaml                     # includes environment_manifest.compose_file: "./environment.compose.yaml"
├── environment.compose.yaml      # postgres + PostgREST + runner + db-service
├── grading.yaml
└── app-db/init.sql
```

That works for one task. For **five** tasks that all want the same
four-service stack, the model duplicates:

```
tasks/
├── task_a/environment.compose.yaml    # postgres + backend + frontend + queue
├── task_b/environment.compose.yaml    # postgres + backend + frontend + queue  ← same
├── task_c/environment.compose.yaml    # postgres + backend + frontend + queue  ← same
├── task_d/environment.compose.yaml    # postgres + backend + frontend + queue  ← same
└── task_e/environment.compose.yaml    # postgres + backend + frontend + queue  ← same
```

Every service definition, every image tag, every port mapping, every
volume binding — five times. Change the postgres version once,
change it five times. And when task C additionally needs an isolated
DB (because it mutates the schema and would contaminate the other
trials), there's no clean way to express "same as the base, except
this one service." The author copies the compose file, edits one
service, and now the redundancy has a fork in it.

### What we actually want

- **Shared base environment** declared once, referenced N times.
- **Per-task variation** expressed as a small delta — flip one
  service to a stricter isolation mode; drop one service; swap one
  service definition entirely.
- **A safe escape hatch** — a task that genuinely needs a bespoke
  environment can still declare a full `environment_manifest`
  without buying into the project layer.

## 2 · What exists to build on

Three pieces already in the codebase point at how the extension can
land cheaply:

- **`EnvironmentManifest` schema** at
  [`tolokaforge/runner/models.py:835`](../../../tolokaforge/runner/models.py) —
  the typed Pydantic model that a manifest becomes at load time.
  Already has three fields declared but not consumed by any
  provisioner (`initial_state`, `network_policy`,
  `security_context_defaults`); the extension can plug into these
  existing seams without a schema break.
- **Shared-domain merge precedent** in
  [`tolokaforge/adapters/_task_loader.py`](../../../tolokaforge/adapters/_task_loader.py) —
  the loader already deep-merges a shared `domain.yaml` into each
  task's `task.yaml` when the task carries a `domain:` reference
  (see the [`native_shared_domain` example](../../../examples/native/native_shared_domain/)).
  It rewrites relative paths from the domain root to the task root
  correctly. The merge machinery is done and reusable; the
  project/recipe extension is a generalisation of the same pattern
  from per-category to per-pack scope.
- **Per-service isolation vocabulary** — the "Alternative A" model
  from the multi-container isolation design (per-service
  `isolation: ephemeral | shared | reset` with a hardcoded reset-
  primitive catalog) provides the vocabulary a project needs to say
  "keep this service warm across trials, reset its state between."
  That work is spec-only today but its schema is directly usable
  here.

## 3 · Industry patterns — five design principles

Ten patterns surveyed for how they handle base-config + per-instance
overlays. Five load-bearing insights emerge:

- **Explicit typed inputs beat implicit merging.** GitHub Actions
  reusable workflows ([docs](https://docs.github.com/en/actions/using-workflows/reusing-workflows)),
  Terraform modules ([docs](https://developer.hashicorp.com/terraform/language/modules)),
  and OpenAPI `$ref` composition ([spec](https://spec.openapis.org/oas/v3.1.0))
  all make the seam explicit — inputs are declared, outputs are
  declared, no silent-merge surprises. GitLab CI's `include` with
  deep-merge on maps ([docs](https://docs.gitlab.com/ee/ci/yaml/includes.html))
  is forgiving but footgun-prone (arrays quietly "last-wins").
  **Adopt:** every project-level input is a typed field; every
  overridable point is named.
- **Field-level metadata > per-file defaults.** Kustomize's
  `//patchMergeKey` / `//patchStrategy` markers
  ([reference](https://kubectl.docs.kubernetes.io/references/kustomize/))
  put merge intent *on the schema*, not in trial-and-error patch
  authoring. **Adopt:** the manifest schema declares per-field
  whether a task recipe can override, and how (replace / append /
  merge-by-key). Not the Kustomize syntax — the principle.
- **Precedence hierarchies 2–3 levels, not deeper.** Inspect AI's
  `eval() > Task > Sample` precedence ([task docs](https://inspect.aisi.org.uk/tasks.html))
  is clear. Ansible's 5+ variable tiers are famously confusing.
  **Adopt:** `Project > Task recipe`. Two levels. Trial-level
  overrides deliberately not in scope.
- **Isolation-via-replacement > isolation-via-patch.** Inspect
  samples replace the sandbox config wholesale when a per-sample
  override is needed
  ([sandboxing docs](https://inspect.aisi.org.uk/sandboxing.html)) —
  no surgical patching. SWE-bench's layered images are immutable;
  each layer is a full snapshot. **Adopt:** when a task needs to
  isolate one service (its own postgres, say), the recipe *replaces
  that service definition entirely*. It doesn't try to patch the
  base service's fields.
- **Profile-like filtering > per-task branching.** Docker Compose
  profiles ([docs](https://docs.docker.com/compose/profiles/))
  activate service subsets via tag selection. Twenty task-specific
  branches become five profiles + task-side selection. **Adopt:**
  project declares service profiles (e.g. `metrics`, `worker`);
  tasks select which profiles they want active.

**Deliberate rejections:**

- Deep-merge on untyped fields (GitLab CI style) — silent array
  collisions.
- Implicit precedence > 3 levels (Ansible style) — hard to debug.
- JSON Pointer patching (Kustomize's `patches`) — precise but
  fragile when the base schema changes.
- Nested profiles / profiles-that-include-profiles — deferred to a
  future revision if evidence justifies.

## 4 · The proposed model

### 4.1 New file: `project.yaml` at the pack root

```yaml
# examples/native/example-project-microservices/project.yaml
project:
  name: "example-project-microservices"
  description: "Sample microservices stack — frontend, backend, DB, queue."

# Typed inputs the project exposes; task recipes may override.
# Every input has a type + default. No untyped values.
inputs:
  postgres_version:
    type: string
    default: "16"
    description: "postgres image tag"
  redis_included:
    type: bool
    default: true
    description: "whether the queue service is materialised"
  isolation:
    type: enum
    values: [ephemeral, shared, reset]
    default: shared
    description: "default per-service isolation stance"

# Base environment manifest — points at the shared compose file.
# The compose file itself uses ${...} substitutions bound to inputs.
environment_manifest:
  compose_file: "./environment/project.compose.yaml"
  runner_service: "runner"
  isolation: "per_trial"

# Named service profiles. A task activates zero or more profiles.
# Each profile is a compose fragment that adds services.
profiles:
  metrics:
    compose_file: "./profiles/metrics.compose.yaml"
    description: "prometheus + grafana; opt-in for tasks that need it"
  worker:
    compose_file: "./profiles/worker.compose.yaml"
    description: "background worker; opt-in"

# Overridable fields — the schema explicitly declares what a task
# recipe may change. Fields not listed here cannot be overridden.
# This is the Kustomize field-metadata insight, without the syntax.
overridable:
  - "services.app-db.isolation"          # per-service isolation stance
  - "services.app-db.image"              # image tag (via postgres_version)
  - "services.*.reset_primitive"         # reset-primitive per service
  - "profiles"                           # which profiles are active
  - "services.*"                         # full service replacement (see §4.3)
```

**Key properties of the schema:**

- `inputs` are typed. A task recipe that tries to set
  `postgres_version` to an integer fails at load time with a clear
  error — no silent coercion.
- `environment_manifest` is a plain `EnvironmentManifest` block
  today (schema unchanged) — the project extension is additive.
- `profiles` are named + independently opt-in. Compose them
  freely. No nesting.
- `overridable` is the schema's declaration of *which* fields a
  task recipe may touch. Anything not in that list is silently
  ignored if a recipe tries to override it — with a warning at load
  time.

### 4.2 New file: `recipe.yaml` (optional, per task)

A task under a project may ship a `recipe.yaml` alongside its
`task.yaml`. If present, the loader applies the recipe on top of the
project defaults; if absent, the task inherits the project as-is.

```yaml
# example: tasks/db_schema_migrate/recipe.yaml
# This task mutates the DB schema. It needs a per-trial-reset DB.
inputs:
  isolation: reset           # override the project default

services:
  app-db:
    isolation: reset
    reset_primitive: postgres_template_db
```

Three things a recipe can do — and only these three:

1. **Override typed inputs** (`inputs.<name>: <value>`). Type-
   checked against the project's declared input types.
2. **Select / drop profiles** (`profiles.active: [metrics]`,
   `profiles.dropped: [worker]`).
3. **Replace specific services** (`services.<name>: <full service
   spec>`). Explicit whole-service replacement, not field patching.
   Replaces the entire service definition from the project.

That's the whole recipe grammar. **No free-form deep merge, no
patches, no path expressions.** If a task wants to change *just* an
image tag on one service, the recipe declares the whole replacement
service. Verbosity is the point — the reader sees exactly what the
task ends up with, without simulating a merge in their head.

### 4.3 Precedence

Two levels, in this order:

1. **Project defaults** — from `project.yaml` (`inputs.*.default`,
   `environment_manifest`, `profiles`).
2. **Task recipe** — from `recipe.yaml` if present. Recipe wins on
   overridable fields; other fields untouched.

At merge time, the loader:

- Binds every input to `recipe.inputs.<name>` if set, else
  `project.inputs.<name>.default`.
- Substitutes bound inputs into the project's compose file
  (`${postgres_version}` → `"16"`).
- Materialises the base compose + activates any profiles from
  `recipe.profiles.active`, subtracting `recipe.profiles.dropped`.
- Applies `recipe.services.<name>` full replacements to the merged
  compose.
- Emits the fully-resolved `EnvironmentManifest` that the runtime
  actually sees.

**No trial-level layer.** A trial gets exactly one merged
manifest per task, computed once at task-load time. Adding trial-
level overrides would push precedence to three levels and is
deliberately out of scope.

### 4.4 Escape hatch — task-declared full manifest

A task that doesn't want the project layer at all just ships its
own `environment_manifest.compose_file` in its `task.yaml`, exactly
as today. The loader recognises this and skips project inheritance
for that task. **Backward compatibility is total** — every existing
task pack works unchanged.

## 5 · Extensibility mechanism — the service-type registry

The above schema handles composition. It doesn't yet handle
extensibility — how a third party adds a *new kind of service* the
project can declare, with its own reset semantics, without editing
the engine.

**The service-type registry.** Each service type is a plugin
identified by a string tag. TolokaForge ships several built-ins
(`postgres`, `redis`, `http-static`, `runner`, `db-service`). New
service types register via Python entry-points under a group like
`tolokaforge.service_types` — the same pattern the engine already
uses for adapters (see [`tolokaforge/adapters/__init__.py`](../../../tolokaforge/adapters/__init__.py)).

A service type declares:

- **Its identity** — the string tag used in `type:` fields in the
  compose overlay.
- **Which isolation modes it supports** (`ephemeral`, `shared`,
  `reset`) — via a class attribute.
- **Its reset primitive**, if any — how to bring the service back
  to a known state cheaply. For postgres this is the template-DB
  clone; for a filesystem-workspace service this is a dir swap;
  for a stateless HTTP static server it's a no-op.
- **Its endpoint discovery contract** — what URLs / hosts / ports
  the service exposes and how a `TaskDescription` binds to them.

**Why it fits the project/recipe design:** the schema stays small
(services are referenced by name in `services.<name>` blocks), but
the *behaviour* of each service type — reset semantics, endpoint
resolution, safety validation — lives in the plugin. A customer
who wants "MongoDB as a resettable service type" doesn't fork the
engine; they ship a `mycompany-mongodb-service-type` package with
one entry-point declaration, and their tasks reference `type:
mongodb` under `services.*`.

This directly composes with the broader Protocol-as-entry-point
direction already spelled out in
`MODULE_INDEPENDENCE_ANALYSIS.md` — the service-type registry is
one more entry-point group added alongside `tolokaforge.adapters`,
`tolokaforge.graders`, `tolokaforge.runtimes`, etc.

## 6 · Worked example — 5 tasks × 4 recipe patterns + 1 escape hatch

See [`examples/native/example-project-microservices/`](../../../examples/native/example-project-microservices/)
for the full spec. Highlights:

| Task | Recipe pattern | What it demonstrates |
|---|---|---|
| `api_endpoint_add` | *no recipe* | Full project inheritance; shortest task.yaml |
| `db_schema_migrate` | isolation modifier | `app-db` flipped to `reset` mode + template-DB primitive |
| `isolated_data_experiment` | full service replacement | Task provides its own `app-db`, keeps rest |
| `observability_alert_config` | profile selection | Activates `metrics` profile, drops `frontend` |
| `legacy_migration_bug` | full escape hatch | Task doesn't use the project — declares own env |

Per-task file counts, without and with the extension:

| Layout | Files per task | Total files (5 tasks) |
|---|---|---|
| Today's model (each task ships own compose) | ~4 (task.yaml, env.compose, grading.yaml, fixtures/) | ~20 + 5 duplicated compose files |
| Proposed (project + recipes) | ~2-3 (task.yaml, optional recipe.yaml, grading.yaml) | ~12 + 1 shared compose + 2 profile fragments |

Compression is real but modest for 5 tasks; the model pays off
super-linearly as task count grows. At 50 tasks the shared compose
is written once instead of fifty times.

## 7 · Backward compatibility

- **Task packs without a `project.yaml` work unchanged.** The
  loader checks for `project.yaml` at pack root; if absent, every
  task loads with today's semantics.
- **Tasks under a project that don't ship a `recipe.yaml`** inherit
  the full project default.
- **Tasks under a project that ship their own `environment_manifest`**
  in `task.yaml` (the escape-hatch case) bypass the project's
  environment inheritance entirely.
- **Existing `native_shared_domain` semantics** (per-category
  `_shared/domain.yaml` merge) continue to work in parallel — that
  merge is about `tools:` and `system_prompt:`, not environment.
  The project layer adds environment inheritance without touching
  domain-level tool inheritance.
- **A migration converter** could be shipped later — a script that
  inspects a task pack's compose files, detects "obvious shared
  shape" (all tasks reference identical services), and emits a
  candidate `project.yaml` + slimmed task-side files. Not required
  for launch.

## 8 · Failure modes to defend against

- **Silent cross-trial contamination.** A `shared` service in the
  project + a task recipe that neglects to override it, when the
  task in fact mutates state. Mitigation: the safe default for
  services under a `project.yaml` is `ephemeral`, matching today's
  `per_trial` behaviour. `shared` is opt-in per service.
- **Profile-name typos.** A recipe activates a profile called
  `metricz` (misspelt). Mitigation: profile activation is validated
  against the project's declared profile list at load time; unknown
  profile names are a hard error.
- **Overridable-field drift.** Project author adds a new field to
  a service and forgets to update `overridable:`. Task recipes
  can't override it; the recipe silently falls back to the project
  value. Mitigation: schema validator warns when a recipe references
  a field not declared overridable, but does not fail — allows soft
  migration.
- **Version-skew between `project.yaml` and `task.yaml`.** The
  project sets `postgres_version: "16"`; a task-level
  `task.yaml.environment_manifest.compose_file` (escape hatch)
  pins `postgres:15`. Mitigation: the escape hatch bypasses the
  project entirely — no version-skew possible because there's no
  merge. The two paths are mutually exclusive per task.
- **Circular profile references.** A profile references itself or
  another profile that references it back. Mitigation: profiles
  are flat (no nesting) in v1. If future evidence justifies
  nesting, cycle-detection becomes the loader's problem.

## 9 · Open questions — the design deliberately defers

- **Default isolation when the recipe is silent about a service.**
  `ephemeral` (safe, expensive) or `shared` (cheap, dangerous)?
  Recommendation: `ephemeral` (fail-loud principle from the
  isolation proposal). Confirm at implementation time.
- **Whether profiles nest.** Should `profile A` be able to
  `include:` another profile? Deferring — one level of profiles is
  enough for the customer scenarios we've seen. Revisit if a real
  customer task pack demands it.
- **How project-level env vars interact with task-level env vars.**
  If the project's compose declares `${DATABASE_URL}` and a task's
  recipe redefines `DATABASE_URL`, does the recipe win? Almost
  certainly yes (Project > Task recipe precedence), but the
  interaction with Docker Compose's own env-var precedence rules
  is worth pinning in an ADR.
- **Whether `recipe.yaml` should live at the task level or be
  inline in `task.yaml`.** Separate file (as proposed) makes the
  recipe visually distinct and easy to grep; inline saves one
  file. Recommendation: separate file, but a `recipe:` inline block
  in `task.yaml` could be accepted as an alternative form.
- **Migration story for tasks whose current `environment.compose.yaml`
  is *nearly* identical to what would become the project default,
  but not exactly.** How does a converter tool handle
  "close-but-not-identical" shapes without either forcing a rewrite
  or silently losing task-specific detail?

## 10 · Sequencing — what ships first if this design is adopted

Ordered smallest-first-slice → biggest strategic payoff:

1. **`project.yaml` schema + loader** — schema-only. The loader
   recognises `project.yaml`, parses it, exposes the resolved
   manifest to the runtime, but does *not* yet implement per-service
   isolation dispatch or reset primitives. One PR against the
   engine; existing tasks unaffected.
2. **`recipe.yaml` overlay logic** — task-side recipe files applied
   over project defaults. Second PR; still schema-plus-loader work.
3. **Per-service isolation implementation** (from the isolation
   proposal's Alternative A) — runtime dispatch based on
   `services.<name>.isolation`. Third PR; the reset primitives ship
   as they become needed.
4. **Service-type registry** — the extensibility mechanism from §5.
   Fourth PR; entry-point group + registry lookup + docs. Ships
   after the schema is stable so the plugin API doesn't churn.
5. **Migration converter script** — optional; ships when a real
   task pack wants to migrate.

Each step is independently landable. Nothing forces the whole
design to ship in one PR.

## 11 · Cross-references

- [`../MULTI_CONTAINER_ISOLATION_PROPOSAL.html`](../MULTI_CONTAINER_ISOLATION_PROPOSAL.html)
  Tab 2 §5 (Alternative A — per-service isolation vocabulary +
  reset primitives) — the isolation vocabulary this design consumes.
  This proposal is the natural extension of Alternative A from the
  task-declared level up to the project level.
- [`../adr/0009-environment-manifest.md`](../adr/0009-environment-manifest.md)
  — the accepted ADR for `EnvironmentManifest` today; the schema
  this proposal extends.
- [`../adr/0018-multi-container-under-shared-runtime.md`](../adr/0018-multi-container-under-shared-runtime.md)
  — the case matrix (built-in vs task-declared × shared vs per-
  trial) that this proposal broadens by adding project-level
  composition.
- Existing multi-service examples:
  [`multi_service`](../../../examples/native/multi_service/),
  [`multi_service_advanced`](../../../examples/native/multi_service_advanced/),
  [`multi_service_postgres`](../../../examples/native/multi_service_postgres/)
  — each of these could be refactored under a project when the
  extension ships, though none *needs* to be (backward-compat).
- [`native_shared_domain`](../../../examples/native/native_shared_domain/)
  — the existing per-category shared-domain example. The project
  layer is a per-pack generalisation of the same pattern.

## 12 · What's out of scope

- **Trial-level environment overrides.** Two levels only.
- **Deep-merge on untyped fields.** Explicit overrides via typed
  inputs / profile selection / service replacement only.
- **Profile nesting.** Flat profiles in v1.
- **Cross-project inheritance.** A project cannot inherit from
  another project. If shared library material is needed, it's a
  service-type plugin.
- **Runtime-level project semantics.** The project layer is a
  load-time construct — by the time a `TrialSpec` reaches the
  runtime, it carries a fully-resolved `EnvironmentManifest` with
  no project awareness. `RuntimeBackend` sees the merged manifest,
  not the project + recipe.
- **Compose-file editing.** The project's base compose file is
  never mutated on disk. All composition happens in memory during
  load.

---

*Return to [`docs/architecture/`](../) for the ADR index and the
canonical runtime-backends documentation.*
