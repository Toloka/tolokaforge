# 0068. Tool artefacts reach a trial-scope runner by reference

- **Status:** Proposed
- **Date:** 2026-11-04
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —
- **Revised:** 2026-11-09, by the addressing of [ADR-0072](https://github.com/Toloka/tolokaforge/pull/1983):
  a reference is a `pack:` address, the delivery is a run setting, and the grader rule
  is written on the trial contract.
- **Revised:** 2026-11-10, ADR-0072's open question 4: the runner's mount holds the referenced
  files only, staged per trial, not the whole task directory.

## Context and Problem Statement

Every file a trial imports or reads from its task — MCP server scripts, tool
modules, `checks.py`, a shared domain's world data, a case's state file, a RAG
corpus — travels base64-encoded in `TaskDescription.tool_artifacts`
(`dict[str, str]`). The description is embedded in the `TrialSpec` JSON that
`RegisterTrial` carries in one unary gRPC message
(`RegisterTrialRequest.trial_spec_json`), and the runner decodes it into the
trial's artefact directory (`core/grading/tool_artifacts.py`). That was sized
for tool code. It does not scale to a task whose world is data.

One native-format task, measured: its world is about 6.5k files, and its
description registers at **1367 MiB**. That is above the 1024 MiB the run had
raised `compute.grpc_max_message_mb` to, and close to the 2047 MiB ceiling of a
signed 32-bit gRPC length. The task declares its own stack (a compose file
shipped in the task directory, `stack_scope: trial`, a runner service and a
database service), so the task directory sits on the disk of the machine that
starts the stack.

Run in a 24 GiB sandbox with no model calls, and sampled once a second:

| Phase | Engine process | Runner container |
|---|---|---|
| Describe (`validate`) | peak 8.6 GiB | — |
| At rest with the built description | 4.6 GiB | — |
| `RegisterTrial`: description serialised and sent | 12.7 GiB after ~1 s, 15.1 GiB after ~4 s, held while the runner receives | 0.2 → 1.1 → 3.7 → 8.7 GiB in ~5 s |
| Kernel OOM kill | killed at 15.1 GiB (total 24.5 GiB) | left at 9.5 GiB |

On a 31 GiB machine the same registration completes, and the runner peaks at
about 12.8 GiB while it decodes the description and loads the world.

The engine pays about 10.5 GiB *on top of* the resident description just to send
it, because every layer of the send holds its own whole copy of the payload:

1. the `str` values of `tool_artifacts` in the cached description (resident);
2. `TrialSpec.model_dump_json()` — pydantic-core's output buffer, then the Python
   `str` it is copied into;
3. `RegisterTrialRequest(trial_spec_json=…)` — the protobuf message's copy;
4. the serialised request `bytes` the gRPC stub sends;
5. gRPC core's send slices and transport buffers.

The runner repeats the chain in reverse: the received bytes, the parsed message,
the `str` field, the validated `TrialSpec` with its own base64 strings, and each
file's decoded bytes while it is written out. Raising the gRPC limit does not
remove a single copy; it only moves the ceiling.

## Decision Drivers

- The bytes of a task's files are not multiplied by the number of layers between
  the adapter and the runner's disk; the engine's and the runner's memory do not
  grow with the world's size.
- The runner uses exactly the bytes the task was described with, or refuses the
  trial; nothing is skipped silently, and a task directory that changed after the
  task was described is caught.
- Opt-in, and a task that sets nothing is described, registered and graded
  byte-for-byte as before.
- No new service, no new RPC, and no change to the runner image beyond the code.
- Each topology that cannot serve the new delivery refuses it before a trial
  spends tokens, not at grade time.

## Considered Options

1. **Raise `compute.grpc_max_message_mb`.** Already at 1024 MiB for this task;
   the ceiling is 2047 MiB. It keeps every copy above, so the engine still holds
   five to six copies of the world, and the next larger world has nowhere to go.
2. **Stream or chunk the registration** (a client-streaming `RegisterTrial`, or
   a separate artefact-upload RPC before it). Removes the size ceiling and most
   transient copies, but adds a protocol surface to the runner and to every
   runtime backend and node that registers trials, and the bytes still cross a
   network the task's files are already next to.
3. **Compress the payload** (gzip of the JSON or per file). Text worlds compress
   well, but every layer still holds a whole (compressed) copy, decompression
   adds a full-size copy on the runner, and the ceiling only moves.
4. **Seed the world through the database service.** Fits data the engine already
   models as `initial_state.tables`, not files an MCP server or `checks.py`
   imports or opens, and it moves the same bytes over HTTP instead of gRPC.
5. **Deliver by reference to a read-only mount.** The adapter records each file's
   path, SHA-256 and size; the engine bind-mounts the task directory read-only
   into the runner service of the trial-scope stack it materialises; the runner
   copies each file into the trial's artefact directory, hashing while it copies,
   and refuses any mismatch.

## Decision

We will adopt **Option 5**, opt-in through the run's configuration, for runners
the engine materialises per trial from a task-declared stack.

- **Declaration.** `compute.trial_data_delivery: inline | mount` in the run
  config (default `inline`). How bytes move is a property of the deployment, not
  of a task, so one task stays comparable across substrates (ADR-0072 D3). It
  applies to a task whose runner a `trial`-scope stack owns — a `run`-scope runner
  serves every task of the run and was started before any of them was described —
  and any other task stays inline, with a log line saying why. A task manifest
  that names the per-task key an earlier draft used is refused, so a pack written
  for it does not silently run inline.
- **Wire.** `TaskDescription.tool_artifact_refs: dict[str, ArtifactRef] | None`,
  keyed by artefact path, each `ArtifactRef {uri, sha256, size}` (ADR-0072 D1).
  The one scheme is `pack:<path>`, relative to the task's directory on whichever
  machine reads it: no path of the describing machine is on the wire, so a
  description built on one machine stays valid where the trial runs. The pack path
  may differ from the artefact path (the native adapter delivers a case's state
  file as `initial_state.json` beside a shared MCP server). Paths are normalised
  relative POSIX paths; an artefact path is inline or referenced, never both.
- **Adapter.** Under `mount` the native adapter references every file it would
  have bundled — the task directory's files, the case state copy, the RAG corpus —
  hashing each in 1 MiB chunks, and puts nothing in `tool_artifacts`. Both
  deliveries take the set from one hook, `_task_artifact_sources`; a subclass
  that overrides the inline bundler alone is refused under `mount`, so its
  references cannot name a different set than its bundle (the RAG corpus's
  `_corpus_artifact_paths` and `_bundle_corpus_artifacts` pair likewise). A file
  that cannot be read fails the description under either delivery. A source
  outside the task directory is refused. Tool libraries (ADR-0060) keep merging
  inline; a library path that collides with a referenced artefact is refused like
  an inline collision.
- **Engine.** The composer locates its own copy of the task directory through the
  run's adapter (`RunSubstrate.task_dir_of`) and hands it to the stack that owns
  the trial's runner (and refuses a description with references when no
  trial-scope stack owns it, or when the run cannot locate the task, before any
  stack starts). It stages the referenced files alone into a per-trial directory
  beside the trial's final-state directory, at their `pack:` paths: a hard link to
  the task's file where the filesystem allows it, a copy otherwise. A file that is
  missing, resolves outside the task directory or has another size than described
  is refused there, every one listed, before any stack starts; the runner's digest
  check stays the one that decides. The directory goes with the final-state one at
  teardown. The docker-compose materialiser adds a read-only bind mount of it at
  `/tolokaforge/tool-artifact-refs` to the runner service and sets
  `TOLOKAFORGE_TOOL_ARTIFACT_REFS_DIR` to it; a compose file that already
  declares the variable or a volume at that path is refused.
- **Runner.** At `RegisterTrial` the runner copies every reference from the
  mount into the trial's artefact directory, next to the inline entries, hashing
  while it copies. A missing file, a source that resolves outside the mount, or a
  digest or size mismatch refuses the registration with every offending path
  listed; the directory is removed and nothing lands on `sys.path`. A runner with
  no mount refuses a description with references. The trial's copy keeps each
  trial isolated from the next, as inline extraction does.
- **Graders.** References resolve only on the trial's runner, so no grader that
  reads the task's files from elsewhere can grade such a task: `grader_rpc`,
  `queue` and snapshot bundles are refused (a grader that reads no world, such
  as `judge_only`, is unaffected). The rule is
  written once on the trial contract (`core/trial_validation.py`, ADR-0072 D4):
  the orchestrator applies it to its run at start, and `run_trial_from_inputs`
  to the one trial it is handed, so the library route refuses `grader_rpc`,
  `queue` and `grader.snapshot.enabled` as the orchestrator does. The detached
  grader, the offline composite kind and the bundle producer refuse a description
  with references as a second line. A store a remote grader can read is ADR-0072's
  follow-up.
- **Protocol.** `ENGINE_PROTOCOL_VERSION` becomes 8 (7 is ADR-0067's). An older runner image
  forbids the new key and refuses every trial spec at `Invalid trial_spec_json`;
  an older engine is refused by the runner's version gate. Neither pairing runs a
  trial whose artefacts did not arrive.

## Consequences

### Positive

- The registration message of the measured task shrinks from 1367 MiB to the
  size of its paths and digests (about 6.5k entries), so none of the copies in
  the chain above scales with the world any more, on either side.
- The runner reads each file once from disk, in chunks, and never holds the
  world in memory to place it on disk.
- A task directory edited between description and trial, or a mount of the wrong
  directory, is refused with the paths and digests at fault.

### Negative / Trade-offs

- The runner service sees the whole task directory read-only, not only the files
  the trial references; a pack that keeps something there the runner must not
  see cannot use `mount`.
- The engine reads the task's files once per description to hash them, and the
  runner hashes them again per trial while copying.
- Each trial copies the referenced files to the runner's disk, as inline
  extraction writes them today.
- Only task-declared trial-scope runners are served. The built-in shared stack,
  task-scope and run-scope runners, `grader_rpc`, `queue` and snapshot grading
  keep inline delivery.
- The mount is the machine that materialises the stack's own copy of the task
  directory, found through its adapter; a node needs the task's pack, as it
  already does for compose files and `task_pack_hash`.
- Protocol 8 requires rebuilding or re-pinning runner images even for runs that
  never use `mount`.

### Follow-ups

- Code changes: the opt-in slice described above lands with this ADR.
- Independent host-side reduction, for inline delivery too: drop the intermediate
  copies at registration — release the JSON string once the request is built,
  serialise the spec straight to bytes instead of to `str`, and avoid holding the
  cached description's artefacts and the serialised request at the same time.
  That trims about two of the copies above without changing the protocol.
- Serving references to the grader service and to snapshot bundles through an
  `ArtifactStore` a remote grader can read (ADR-0072 D2), if a task that needs
  `mount` also needs detached grading.
- A world seeded into the database as `initial_state.tables` still travels
  inline; it is not a tool artefact.
- `copy_compose_context` copies the compose file's whole directory into every
  trial's context; a pack whose compose file sits beside a large world copies
  that world once per trial. Copy the compose file and its relative bind
  sources only.
- Documentation updated: `TASK_DESCRIPTION_SCHEMA.md`, `GRPC_PROTOCOL.md`,
  `MULTI_CONTAINER_GUIDE.md`, `NATIVE_ADAPTER.md`.
- Tests added: wire validation, verified copy and every refusal, `RegisterTrial`
  with and without the mount, resolver scope rules, the native adapter's
  references against its inline bundle, the compose mount, the composer's root
  hand-off and refusal, the grader refusals and the run-start gate. A run against
  a real Docker stack is not part of the unit suite.

## Links

- Related ADRs: [0044](0044-composition-plan-runtime.md) (stack scopes), [0060](0060-shared-tool-libraries-through-tool-artifacts.md) (tool libraries through `tool_artifacts`).
- Related code: `core/grading/tool_artifacts.py`, `adapters/native.py`, `core/compose_materialisation.py`, `core/default_substrate_composer.py`, `runner/service.py` (`RegisterTrial`), `runner/protocol.py`.
