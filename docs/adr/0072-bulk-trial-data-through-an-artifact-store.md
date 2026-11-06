# 0072. A trial's bulk data moves through an artifact store

- **Status:** Proposed
- **Date:** 2026-11-06
- **Deciders:** @CiroGamboa (drafted for team discussion; the measurements and the
  mechanism are @rsmtnn's, from ADR-0068/0069/0070/0071)
- **Supersedes:** — (if accepted: ADR-0068, ADR-0069 and ADR-0071, all unmerged.
  ADR-0070 is unaffected and should land on its own.)
- **Superseded by:** —

> Numbers 0068-0071 are taken by the four unmerged ADRs on the branches of
> #1970, #1972, #1977 and #1978. This ADR takes the next free number rather
> than reusing one of theirs.

## Context and Problem Statement

A native-format task whose world is data cannot run a trial. Measured by
@rsmtnn on one task of about 6.5k files and an initial state of about 490 MB of
JSON, in a 24 GiB sandbox with no model calls:

- its `TaskDescription` registers at **1367 MiB**, against a 2047 MiB ceiling;
- the engine holds **five to six whole copies** of the payload to send it (the
  cached description's `str`s, pydantic's dump buffer, the Python `str`, the
  protobuf message, the serialised bytes, gRPC's send slices), reaching
  15.1 GiB before the kernel OOM-kills it;
- the return direction is worse: between "Trial graded" and "Trial output
  saved" the host spent **6 min 20 s** and grew **4.4 → 11.4 GiB** writing an
  `env.yaml` that no grader reads.

That diagnosis is not in question, and neither is the mechanism it implies:
**keep digests in messages, move bytes out of band, verify on arrival.** This
ADR adopts all of it.

What is in question is the *vocabulary* ADR-0068/0069/0071 wrap it in. They add
four task-manifest fields — `tool_artifacts_delivery`, `final_state_delivery`,
`initial_state_delivery`, each `inline | mount` — where `mount` means one
concrete thing: a Docker bind mount from a filesystem the engine process and
the runner container already share. Three assumptions enter the task contract:

1. the engine host and the runner share a filesystem;
2. the materialiser is docker-compose;
3. the consumer of the data is the trial's own runner.

Three forces make those assumptions costly now rather than later:

**The planned substrates break assumptions 1 and 2.** Kubernetes, Daytona and
Modal remote sandboxes, and object storage as a data plane are all on the
roadmap. ADR-0044 anticipated them ("K8s / Modal materialiser (register in
`tolokaforge.compose_materialisers`)"), and a materialiser registered through
that seam cannot honour the word `mount`.

**Queue grading breaks assumption 3.** ADR-0068 and ADR-0069 refuse
`grader_rpc`, `queue` and `grader.snapshot.enabled` for a `mount` task, because
a host path means nothing to a grader elsewhere. Detached and queued grading
for large worlds is a goal, not a concession — so those refusals are debt, not
scope.

**The feature reaches one of six shipped topologies.** `mount` is refused
unless a task-declared **trial-scope** stack owns the runner. That excludes the
built-in shared stack, task-scope stacks, the five-container
`deploy/standalone/` stack, the `deploy/single-container/` appliance, and the
terminal-bench adapter permanently — its `engine` stack is `run`-scope and owns
`runner_service` (`external_adapters/…/adapter.py:523-549`).

Two further forces are about this repository rather than this feature:

- **The precondition being checked is a proxy.** "Is the stack trial-scope?"
  stands in for "can the substrate serve bytes out of band." They coincide on
  docker-compose today. A Daytona or K8s trial-scope stack satisfies the former
  and not the latter, so it would be accepted and then fail at trial time.
- **ENGINE_PROTOCOL_VERSION is lockstep.** The engine refuses a runner below its
  constant (`shared_stack_runtime.py:455`) and the runner refuses an engine
  below the same constant (`runner/service.py:1161`); the handshake carries no
  feature list (`runner.proto:104`). Seven bumps exist across the project's
  life. The three ADRs add three more — each invalidating every runner image,
  for optional fields that default to null.

Meanwhile the shape this problem needs already exists in-tree, four times over,
and none of the three ADRs reaches for it:

- **`BundleStore`** (`core/grading/bundle_store.py`) — content-addressed
  `bundle://<store>/<sha256>`, a Protocol of `put`/`get`/`close`/`probe`, with
  `LocalDiskBundleStore` **and** `S3BundleStore` shipped, discovered through
  `tolokaforge.bundle_stores`, purity locked by `bundle-store-purity`.
- **`GradeRequest.bundle_parts_uri`** (field 14) — a `bundle://` URI, reserved
  since grader wire v3, documented as "no in-tree caller populates it yet".
  ADR-0068 adds a refusal beside it instead of populating it.
- **`Orchestrator._validate_snapshot_mode_compatibility`**
  (`core/orchestrator.py:1952-2057`) — the house pattern for exactly this gate:
  config check, then capability probe, then `store.probe()` reachability.
- **`grpc_node` dispatch** — ships a `task_pack_hash` and rebases compose paths
  node-side (`core/trial.py`, `_ComposePathRebase`); `task_pack_dir` is
  documented as "node-local filesystem placement the node supplies, **not
  portable data**". Send a hash, let the receiver resolve the path.

ADR-0068 sends `tool_artifact_refs.root`, an absolute path from the machine that
described the task, and admits the consequence: "a node that materialises the
stack itself must describe the task from its own copy of the pack."

## Decision Drivers

- The bytes of a world are not multiplied by the layers between the adapter and
  the runner's disk, and neither process's memory grows with the world. *(ADR-0068's
  driver, kept verbatim.)*
- The runner uses exactly the bytes the task was described with, or refuses the
  trial. *(ADR-0068's driver, kept verbatim.)*
- A task reads the same on every substrate. How bytes travel is a property of
  the run, not of the task — otherwise one task is no longer comparable across
  substrates, which is the point of a benchmark harness.
- A substrate that cannot serve the delivery is refused **at run start, by
  asking the implementation**, not by inferring it from stack scope.
- No capability is permanently refused in order to ship the delivery.
  `grader_rpc`, `queue` and snapshot bundles must have a route, even if it is
  not the first one built.
- Protocol evolution for optional fields is additive. One version for the whole
  family, not one per direction.
- Reuse the seams this repository already has rather than adding parallel ones.

## Considered Options

1. **The `mount` enum family as proposed** (ADR-0068 + 0069 + 0071) — four
   task-manifest fields, a host path and a per-trial host directory on the wire,
   a scope-based gate, three breaking protocol bumps, and permanent refusals for
   every grading path but `runner_rpc`.
2. **Raise the gRPC limit, or compress the payload.** Rejected by ADR-0068 and
   for its reasons: every layer keeps its own whole copy, decompression adds
   one, and the ceiling only moves.
3. **Stream the RPCs** (client-streaming `RegisterTrial`, server-streaming
   `GetState`). Rejected by ADR-0068/0069 and for their reasons: it removes the
   one-message ceiling but the host still reassembles and parses the state, and
   every runtime backend and node gains a streaming surface. It also keeps the
   bytes crossing a network they are already next to.
4. **One content-addressed reference type plus an `ArtifactStore` seam**, with a
   local-mount store as the first implementation and delivery chosen by the run.
   Same mechanism as option 1, different vocabulary and a different gate.

## Decision

Adopt **option 4**. Keep every byte of @rsmtnn's mechanism — the chunked
hashing, the verify-on-copy, the atomic temp-file-then-rename state write, the
streamed `env.yaml`, the refusal with every offending path named. Change what
names it and what gates it.

### 1. One wire type for all three directions

```
ArtifactRef { uri: str, sha256: str, size: int }
```

One type for the task's files, the initial state and the final state. `uri`
carries the scheme — `mount://`, `s3://`, `bundle://…`. The digest and size are
load-bearing and unchanged from ADR-0068/0069/0071. A reference is never both
inline and by reference, as before.

Consequence: **one additive protocol version for the whole family** instead of
8, 9 and 10. Together with the handshake change below, an older runner can
refuse precisely the stores it cannot serve instead of refusing every trial
spec.

### 2. An `ArtifactStore` seam, modelled on `BundleStore`

A new entry-point group, `tolokaforge.artifact_stores`, beside the twenty-four
that exist, with `BundleStore`'s shape:

```
class ArtifactStore(Protocol):
    name: str
    def put(self, local_path: Path) -> str: ...      # → uri
    def get(self, uri: str, dest: Path) -> Path: ...
    def probe(self) -> None: ...
    def close(self) -> None: ...
```

Ship one, reserve one:

- **`MountArtifactStore`** — #1970/#1972/#1978's code, behaviour-identical.
  `put` returns a `mount://` URI without copying; `get` is the verified chunked
  copy already written and tested. Same bind mounts, same refusals, same
  measured numbers.
- **`S3ArtifactStore`** — later, with `S3BundleStore` as the template and
  `boto3` already an optional extra. **Not** part of this ADR's slice; see Open
  questions.

This is the repository's own idiom. Besides `BundleStore`, the terminal-bench
adapter ships `SkillDelivery` and states the principle in one line:
"`skills_dir_target` says *where* the bundle lands; `SkillDelivery` says *how* it
gets there … an embedder driving a different runtime passes its own." ADR-0040
also already reserved the two substrates for this problem:
`SharedMountGradingSubstrate` (noting "grader and runner must be on the same
host") and `TrajectoryStorageGradingSubstrate`.

### 3. Delivery is a run-level decision

Drop the four `*_delivery` manifest fields. A task declares **what it is** —
a world of files, a `json_db` that names a file. The run declares **how bytes
move** — which store, on which substrate.

In particular, `tool_artifacts_delivery` does not belong in
`_POLICY_REQUEST_FIELDS` (`core/project_loader.py:795`), whose own docstring
defines that category as "policy requests that are **substrate-neutral** (they
describe the trial regardless of substrate)". A field refused on three of four
substrate shapes is not substrate-neutral, and filing it there creates a
concrete failure: a project default of `mount` **survives** a task's atomic
`stack` replacement with a run-scope stack, and the task is then refused
because of a flag it never wrote, inherited from a substrate it replaced.

If a per-task hint proves necessary, add one honest declaration
(`bulk_data: true`), not four transport enums.

### 4. Capability negotiation, not a scope proxy

Replace `_refuse_a_mount_no_trial_runner_can_take` with the pattern
`_validate_snapshot_mode_compatibility` already establishes, in its order:

1. **config** — is a store configured for this run;
2. **capability** — ask the materialiser/backend whether it can serve that store
   to this runner; `NotImplementedError` refuses at run start, naming the
   backend class and the ones that do support it;
3. **reachability** — one `store.probe()`, one actionable message.

Add a `features` / `supported_stores` list to `RegisterTrialResponse` so the
runner **states** what it can serve instead of being inferred from one integer.

Then a K8s or Daytona materialiser becomes a registration, not a new refusal —
which is what ADR-0044 designed the seam for.

### 5. Grading follows the data by address

Populate `GradeRequest.bundle_parts_uri`. Then:

- `runner_rpc` — unchanged, and still the fast path when the data is local;
- `grader_rpc` / `queue` — resolve the same URI through their own store handle;
- snapshot bundles — reference parts rather than embedding them.

Large-world tasks become gradable off the runner because the address is
portable. The refusals ADR-0068/0069 add at run start become **store-dependent**
— a `mount://` URI is still refused for a grader on another host, with the same
message — rather than permanent properties of a large world.

### 6. Take the independent wins first, unblocked

Three pieces of the stack are independent of all of the above and should land on
`main` on their own, before this ADR is settled:

- **#1974** — the DB-init timeout fix (`init_trial` takes the whole-state read
  budget; the error names the exception type);
- **#1977 / ADR-0070** — the redundant grading copies: digest-based mirror sync,
  a judge diff that descends into a field too large to show, per-table mutation
  cost. No protocol change, no new vocabulary, and a documented fallback to
  older db-service images;
- **#1972's first commit** — `core/streaming_yaml.py`. It helps **every** run,
  inline included: 167 s / +2019 MiB becomes 18 s / +0 MiB on the measured
  synthetic state.

## Consequences

### Positive

- The measured task's registration message, host memory and grading peaks
  improve exactly as ADR-0068/0069/0070/0071 measured — the mechanism is theirs
  and is unchanged.
- One additive protocol version for the family instead of three breaking ones;
  runner images are not invalidated three times for fields that default to null.
- A K8s, Daytona or Modal materialiser is a registration against an existing
  seam. The `MaterialiseContext` contract does not gain `root: Path` and
  `final_state_dir: Path`, two fields only a local filesystem can satisfy, which
  every future materialiser would inherit (and which
  `tests/canonical/test_composition_runtime_protocols.py` would pin).
- `grader_rpc`, `queue` and snapshot bundles keep a route to large worlds.
- A reference is portable: a description built on one worker does not name a
  path that exists only there. This matters for queue- and Temporal-coordinated
  distribution, and it is how `grpc_node` already behaves.
- Content-addressed dedupe comes free, as it does for bundles: two identical
  puts land on one URI.
- Redaction has a proper home later — a streaming transform on `put` (a JSON
  tokenizer rewriting values under sensitive keys as bytes pass through) instead
  of withholding the state file whole.

### Negative / Trade-offs

- **Rework on work already written and reviewed twice.** #1970, #1972 and #1978
  are ~7,300 lines with ~130 new tests. Most of it survives as
  `MountArtifactStore`, but the manifest fields, the resolver gate, the
  `MaterialiseContext` fields and the three version bumps change, and the ADRs
  are rewritten. This is a re-pointing, not a rewrite — but it is not free, and
  it delays the Daytona case by the length of that rework.
- **A seam has a cost even with one implementation.** Until `S3ArtifactStore`
  exists, `ArtifactStore` is one Protocol wrapping one local-mount
  implementation, and the indirection buys nothing measurable today. The bet is
  on the second implementation arriving.
- **Two dead seams have to be fixed first** (below), which this ADR's slice does
  not otherwise need.
- **Local mount keeps its own limits.** `mount://` still requires a shared
  filesystem, still exposes a directory to the runner service, and is still
  refused for a grader on another host. The seam does not remove those; it
  stops them being the *only* answer.
- **`env.yaml` still forks by delivery** in the first slice. ADR-0069 is right
  that `OUTPUT_FORMAT.md:659-660` declares the file free-form with no schema
  version, so this is not a contract break — but the same
  `get_final_state()` dict is also a structured grading input on the
  core/`validate` lane (`core/grading/combine.py:128-132`,
  `extract_db_state` in `state_checks.py:250-260`), and
  `docs/ADAPTERS.md:36-44` records the bug that duality caused once already.
  Name both shapes in `OUTPUT_FORMAT.md` and reach the grading shape only
  through the new reader.

### Prerequisites this ADR depends on

Neither is @rsmtnn's doing; both block the extensibility this ADR claims.

- **No way to select a materialiser.** `load_compose_materialiser` /
  `load_substrate_composer` are called only from tests
  (`tests/canonical/test_builtin_plugin_registrations.py:385,402`); production
  hardcodes `DefaultSubstrateComposer.materialiser = DockerComposeMaterialiser()`
  (`core/default_substrate_composer.py:94`) with no config field. The group has
  one member. Until that is wired, "a different materialiser can serve this
  differently" is not available in production.
- **`grader_address` is never populated in-tree** (declared
  `plugin_registry.py:514`, read `trial_grader.py:1027`, `:1303`), so every
  `grader_rpc` / `queue` run dials the *runner's* address —
  `deploy/standalone/examples/grader_rpc/run_config.yaml:15-18` says so.
  Grading is not yet genuinely detached. Since queued grading is the goal, this
  has to be plumbed regardless.

### Open questions for the team

1. **Is object storage fast enough to be the second implementation?** The whole
   argument for the seam rests on `S3ArtifactStore` being worth having, and
   **nobody has measured it.** A 490 MB state per trial through S3, twice
   (put + get), could add minutes per trial and make `mount` the only delivery
   fast enough in practice. If so, this ADR still holds — the seam is how both
   coexist — but the roadmap claim weakens, and a locality-aware store
   (CAS with a local cache, as `BundleStore` already dedupes by digest) becomes
   the real target rather than plain S3. **Measuring this should gate the ADR.**
2. **Should the engine protocol's "forbid unknown keys" gate be relaxed?** It is
   what turns additive optional fields into breaking changes. The grader wire
   does the opposite — stable additive field numbers, a reserved 8-12 window,
   proto3 empty defaults — and is better for it. Worth its own ADR.
3. **Should the mount expose only the referenced files?** ADR-0068 notes the
   runner service sees the **whole** task directory read-only and hands the
   problem to pack authors with no enforcement. A staging directory of hardlinks
   to the referenced files costs little and removes the class of mistake (a
   pack keeping solutions or golden outputs beside its data). Recommended
   regardless of which option wins.
4. **Is the per-trial final-state directory's `0777` acceptable?** ADR-0069 needs
   it so a non-root runner image can write. Fine in a single-tenant sandbox,
   not on a shared CI host. A store seam makes it the store's problem, where a
   matched uid or a named volume solves it properly.
5. **One task-dir mount or two?** `build_task_pack_mount_plan`
   (`core/mounts.py:44-60`, applied `docker/stacks/core.py:220-222`) already
   bind-mounts task-pack roots read-only for the core stack. ADR-0068 adds a
   second, differently shaped one for trial-scope stacks. Consolidate, or say
   why they stay separate.

### Follow-ups

- Code changes: `ArtifactRef` in `runner/models.py`; new
  `core/artifact_store.py` (Protocol + `MountArtifactStore`, modelled on
  `core/grading/bundle_store.py`); `ARTIFACT_STORES_GROUP` in
  `core/plugin_registry.py`; the run-start gate in `core/orchestrator.py`
  following `_validate_snapshot_mode_compatibility`; a store handle on
  `MaterialiseContext` in place of two paths; the verified copy paths of
  `runner/service.py`, `runner/final_state_file.py` and
  `runner/initial_state_file.py` behind the seam; `ArtifactRef` and the
  handshake capability list in `runner/runner.proto`.
- Documentation to update: `GRPC_PROTOCOL.md` (one ledger sentence, not three),
  `TASK_DESCRIPTION_SCHEMA.md`, `OUTPUT_FORMAT.md` (both `env.yaml` shapes),
  `MULTI_CONTAINER_GUIDE.md`, `NATIVE_ADAPTER.md`, `GRADER_SERVICE.md`,
  `RUNTIME_BACKENDS.md`, and this directory's index.
- Tests to add: everything #1970/#1972/#1978 already test, re-pointed at the
  store; plus a purity contract for the new seam in `.importlinter`, the way
  `bundle-store-purity` guards `BundleStore`; plus a canonical test that the
  handshake capability list and the ledger stay level.
- **Not covered by the unit suite, and unverified in all five PRs:** a real
  Docker trial-scope run — a non-root runner image writing into the bind mount,
  file ownership on a Linux host, and a daemon able to bind-mount the engine's
  temporary directory. The memory numbers are credible; the plumbing is not yet
  proven. Re-run the 24 GiB Daytona trial before anything in this family merges.

## Links

- Related ADRs: [0044](0044-composition-plan-runtime.md) (stack scopes and the
  three detachable adapter Protocols — the seam this ADR argues was bypassed),
  [0040](0040-standalone-grader.md) (`GradingSubstrate`; reserves
  `SharedMountGradingSubstrate` and `TrajectoryStorageGradingSubstrate`),
  [0060](0060-shared-tool-libraries-through-tool-artifacts.md) (tool libraries,
  which stay inline), [0063](0063-whole-trial-node-service-and-dispatch-seam.md)
  and [0064](0064-dispatch-adapter-seam.md) (the remote-execution story whose
  content-hash discipline this ADR follows), [0067](0067-tool-error-is-its-own-status.md)
  (protocol 7, the last bump on `main`).
- Superseded if accepted: ADR-0068 (#1970), ADR-0069 (#1972), ADR-0071 (#1978).
  ADR-0070 (#1977) is independent and should land regardless.
- Related code: `core/grading/bundle_store.py`, `core/orchestrator.py`
  (`_validate_snapshot_mode_compatibility`), `core/project_loader.py`
  (`_POLICY_REQUEST_FIELDS`), `core/composition_runtime.py`
  (`MaterialiseContext`, `ComposeMaterialiser`), `core/trial.py`
  (`_ComposePathRebase`, `task_pack_hash`), `core/mounts.py`,
  `grader/grader.proto` (`bundle_parts_uri`), `runner/protocol.py`
  (`ENGINE_PROTOCOL_VERSION`).
- External references: PRs #1970, #1972, #1974, #1977, #1978.
