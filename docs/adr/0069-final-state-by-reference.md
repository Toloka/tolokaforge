# 0069. A trial's final state reaches the engine host by reference

- **Status:** Proposed
- **Date:** 2026-11-05
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

ADR-0068 took a task's world off the registration message: the runner reads
the task's files from a read-only mount. The reverse direction still carries
the whole world. When a trial ends, the conductor's `_capture_final_state`
calls the runner's `GetState`, whose `GetStateResponse.state_json` is the trial's
entire database state as one JSON string in one unary message. The engine
parses it into a dict, `EnvironmentState.get_final_state` derives two more deep
copies of it (`agent`, and the legacy `db`), and `OutputWriter.write_env_state`
hands the result to `yaml.dump` with the pure-Python dumper, which builds a
PyYAML `Node` for every scalar of both copies before it writes a byte.

The same native-format task ADR-0068 measured (a world of about 6.5k files and
a large database), run end to end with ADR-0068's engine and real models in a
24 GiB sandbox, sampled once a second:

| Phase | Engine process | Other processes alive |
|---|---|---|
| Trial execution finished → graded (`GetState` read and parsed) | +1.3 GiB | runner, MCP server, database service |
| "Trial graded" → "Trial output saved" (`write_env_state`) | 4.4 → 11.4 GiB, **6 min 20 s** | runner 3.8 GiB, MCP server 4.8 GiB, database service 3.3 GiB |

About 23.3 GiB of the 24 GiB were anonymous memory at the peak, and the cgroup
hit `memory.max` 3424 times — no OOM kill, about 1 GiB short of one. The trial
had been graded six minutes earlier: the runner grades from its own state
(`GradeTrial`), so nothing in the engine needed the state it spent those
minutes and gigabytes on, except the bundle file.

Measured without Docker (`scripts/analysis/bench_final_state_write.py`, one
fresh process per variant, on a 16 GiB laptop, a synthetic state of 40 tables of
mixed-scalar records whose JSON is 89.6 MiB; inline, `env.yaml` carries it twice
as `agent` and `db` — 190 MiB of YAML — and the process holds both copies,
857 MiB at rest):

| Host-side write of the final state | Wall time | Peak growth over rest | Process at rest |
|---|---|---|---|
| `yaml.dump`, pure-Python dumper (before) | 167.4 s | +2019 MiB | 857 MiB |
| streamed events, Python emitter | 95.3 s | +0 MiB | 858 MiB |
| streamed events, libyaml emitter (inline after this ADR) | 18.3 s | +0 MiB | 858 MiB |
| by reference: verify + copy + 200-byte `env.yaml` (`mount`) | 0.18 s | +1 MiB | 68 MiB — the state is never in the process |

On the runner's side, writing the same state as a file one table at a time
took 0.64 s and +14 MiB over the built state; `json.dumps` of it into the
inline `state_json` took 0.50 s and +101 MiB, before gRPC's own copies.

## Decision Drivers

- The engine host never holds a trial's final database state to put it on
  disk, and no single gRPC message carries it.
- The bundle still records the final state, byte-for-byte as the runner
  reported it, and a file that does not match what the runner reported is
  caught rather than recorded.
- Opt-in, on the same footing as ADR-0068, and a task that sets nothing is
  captured and written as before.
- No host-side consumer silently reads a state it was not given: every
  consumer that needs the state whole is refused before a trial spends tokens.
- What a redaction policy promises about the bundle still holds.

## Considered Options

1. **Raise the gRPC message limit.** Removes nothing: the host still parses the
   string, copies the dict twice and builds the YAML node graph.
2. **Write `env.yaml` with a faster dumper only** (`CSafeDumper`, or JSON-as-YAML).
   `CSafeDumper` still represents the whole document as Python nodes first, so
   memory does not move; JSON is not a safe subset for PyYAML (YAML 1.1 reads
   `1e-05` as a string). A streamed event writer fixes the write (table above) but
   the host still receives, parses and copies the state.
3. **Stream `GetState`** (server-streaming chunks). Removes the one-message
   ceiling, but the host still reassembles and parses the state to write it, and
   every runtime backend and node gains a streaming surface.
4. **Read the state from the database service directly.** Moves the same bytes
   over HTTP into the same host process; the runner's sync of the MCP subprocess
   state into the database (`_refresh_db_mirror`) would have to be duplicated.
5. **Deliver by reference to a writable mount.** The engine bind-mounts a
   per-trial writable directory into the runner; the runner writes the state there
   as a file and answers with its path, SHA-256 and size; the host checks the file
   without parsing it and copies it into the bundle.

## Decision

We will adopt **Option 5**, opt-in through the task's environment, for runners
the engine materialises per trial from a task-declared stack, and, independently,
**Option 2's streamed writer** for every `env.yaml`.

- **Declaration.** `environment_manifest.final_state_delivery: inline | mount`
  (default `inline`), on `EnvironmentPatch` and `EnvironmentManifest`, a
  policy-request field that survives a task's atomic `stack` replacement. It is a
  field of its own rather than a second meaning of `tool_artifacts_delivery`: the
  two directions are independent (a task can have a small world and a large final
  state, or the reverse), they refuse different consumers, and only this one
  interacts with redaction. The resolver applies ADR-0068's rule to both fields:
  `mount` needs a `trial`-scope stack to own the runner.
- **Mount.** The composer makes a fresh host directory per trial
  (`mkdtemp`, private to the engine's user, with the mounted directory itself
  writable by any user so a runner image with another user can write into it),
  hands it to the stack that owns the runner (`MaterialiseContext.final_state_dir`)
  and records it on `ComposedEnvHandle.final_state_dir`; it removes it at
  `teardown_trial` and on a failed provision. The docker-compose materialiser adds
  a writable bind mount of it at `/tolokaforge/final-state` to the runner service
  and sets `TOLOKAFORGE_FINAL_STATE_DIR`; a compose file that already declares the
  variable or a volume at that path is refused, as in ADR-0068. The composer
  refuses `mount` when no trial-scope stack owns the runner, before any stack starts.
- **Wire.** `GetStateRequest.state_by_reference` (field 4) and
  `GetStateResponse.state_file` (field 6, `StateFileRef {path, sha256, size}`).
  The runner writes the state to `final_state.json` in its directory — one
  top-level key at a time with the C JSON encoder, hashing as it writes, under a
  temporary name renamed into place — so the file's bytes are exactly the
  `state_json` an inline answer would carry. A runner without the mount refuses
  the read; `state_json` stays empty.
- **Engine.** The conductor asks by reference only for a task that declares
  `mount` and provisions a database. The runtime backend places the reported
  relative path under its side of the mount (refusing a path that leaves it);
  the conductor checks size and SHA-256 in 1 MiB chunks and never parses the file.
  A failed read or a mismatch records `final_state_error`, as an inline capture
  does. `trajectory.final_env_state` then carries the entries that do not come
  from the database (`filesystem`, mock-web and RAG references,
  `agent_visible_dir`, `environment`) and `final_state: {file, sha256, size}` in
  place of `agent`, `user` and `db`.
- **Bundle.** `OutputWriter.write_env_state` writes that small `env.yaml` and
  copies the runner's file beside it as `final_state.json`, checking it again as
  it copies and renaming into place only on a match. The bundle generation becomes
  9. `read_final_env_state(trial_dir)` gives an offline reader the inline shape —
  it checks the file against the reference, parses it and rebuilds `agent`,
  `user` and `db` exactly as `get_final_state` does.
- **Redaction.** A redacting policy (`SensitiveKeyRedaction`) rewrites values
  under credential-named keys at any depth, which needs the document parsed — the
  one thing the delivery exists not to do. So under a redacting policy
  `final_state.json` is **withheld** and named in the stamp's `omitted`, the rule
  the judge sidecars already follow; `env.yaml` (small, still redacted) keeps the
  reference, and the reader refuses the bundle with the policy named. A run that
  needs a redacted copy of the state delivers it inline. We did not refuse the
  combination at run start: the trial is still graded on the runner and the rest
  of the bundle is still useful.
- **Consumers.** Nothing on the engine host grades from `final_env_state` during
  a run (the runner grades from its own state). The consumers that read the final
  state whole are refused for a `mount` task: `grader_rpc` and `queue` (they read
  it over the wire in one message) and `grader.snapshot.enabled` (a grade bundle
  embeds it) at run start, naming every offending task; the detached grader's
  dispatch and the snapshot bundle producer refuse as a second line. The
  `runner_rpc` grader is unaffected.
- **Protocol.** `ENGINE_PROTOCOL_VERSION` becomes 9. An older image would drop the
  unknown request field and answer inline — the copy the host must not make — and
  forbids `final_state_delivery` in the trial spec; an older engine is refused by
  the runner's version gate. Both pairings fail at registration.
- **Streamed `env.yaml`, for inline delivery too** (a separate change on this
  branch, taken alone if wanted). `tolokaforge.core.streaming_yaml` walks the
  mapping and hands the emitter one event at a time; scalars are rendered as
  `SafeRepresenter` renders them, other values go through `yaml.Dumper`'s
  representers, and libyaml's emitter is used when PyYAML has it. With the Python
  emitter the bytes equal `yaml.dump`'s; with libyaml they read back equal under
  `yaml.safe_load`. Under `NoRedaction` the mapping is no longer copied first.

## Consequences

### Positive

- For a `mount` task the host's memory does not grow with the final state: it
  holds a 1 MiB buffer to hash and copy the file, and an `env.yaml` of a few
  hundred bytes (synthetic state above: 0.18 s and +1 MiB, against 167 s and
  +2.0 GiB on top of two resident copies). The +1.3 GiB at capture and the
  +7 GiB, 6-minute write of the measured trial both go; what is left is one
  sequential read to verify and one read-plus-write to copy.
- No gRPC message carries the state, so the message limit stops bounding the size
  of a world's final state.
- The bundle's `final_state.json` is the runner's bytes, checked twice against the
  digest the runner computed while writing them.
- Every `env.yaml` — inline included — is written without the node graph:
  on the synthetic state above, 167 s and +2.0 GiB become 18 s and no growth.

### Negative / Trade-offs

- The bundle's shape differs by delivery: an inline `env.yaml` has `agent`,
  `user` and `db`; a `mount` one references `final_state.json`, which holds the
  `db` view only. A reader that loads `env.yaml` directly must handle the
  reference or call `read_final_env_state`.
- Under a redacting policy the state is not in the bundle at all.
- The runner still holds the state it reads from the database service, as it
  does inline; it writes it one table at a time, so its peak grows by one table's
  JSON instead of the whole string (+14 MiB against +101 MiB above).
- Live-tracing attachments read every top-level bundle file whole; under `mount`
  that is `final_state.json` instead of a larger `env.yaml`, so the exposure is
  unchanged rather than removed.
- The final-state directory is in the engine's temporary directory, on the
  machine that materialises the stack; a Docker daemon that cannot bind-mount from
  there cannot serve `mount`.
- Protocol 9 requires rebuilding or re-pinning runner images even for runs that
  never use `mount`; bundle generation 9 is stamped on every bundle.

### Follow-ups

- Code changes: the streamed writer and the opt-in slice described above land
  with this ADR.
- The runner's own copy: write the state from the database service's response
  stream instead of a parsed dict, so the runner's peak does not scale either.
- Inline capture still parses the state and copies it twice
  (`get_final_state`'s `agent` and `db`); `agent` could be written from `db`
  without a second copy.
- Serving `grader_rpc` and snapshot bundles a state by reference (the grader
  mounting the same directory, or a bundle part that is the file), if a task that
  needs `mount` also needs detached grading.
- A streaming key-name redaction of the file (a JSON tokenizer that rewrites
  values under sensitive keys as it copies), if a run needs both `mount` and a
  redacted state.
- Live-tracing attachments: stream large files instead of reading them whole.
- Documentation updated: `GRPC_PROTOCOL.md`, `OUTPUT_FORMAT.md`,
  `MULTI_CONTAINER_GUIDE.md`, `TASK_DESCRIPTION_SCHEMA.md`, `GRADER_SERVICE.md`.
- Tests added: the state file (bytes, digest, atomic write, path and symlink
  refusals, verification and copy mismatches), `GetState` by reference with and
  without the mount, the client's and backend's by-reference read, the resolver,
  the compose mount and its refusals, the composer's directory lifecycle and
  refusal, the conductor's capture and every lost-capture path, the bundle writer,
  reader, redaction withholding, and the grader, bundle-producer and run-start
  refusals; the streamed writer against `yaml.dump`. A run against a real Docker
  stack is not part of the unit suite.

## Links

- Related ADRs: [0068](0068-tool-artifacts-by-reference.md) (the forward direction), [0044](0044-composition-plan-runtime.md) (stack scopes).
- Related code: `runner/final_state_file.py`, `runner/service.py` (`GetState`), `core/conductor.py` (`_capture_final_state`), `core/output_writer.py` (`write_env_state`), `core/streaming_yaml.py`, `core/default_substrate_composer.py`, `core/compose_materialisation.py`, `runner/protocol.py`.
