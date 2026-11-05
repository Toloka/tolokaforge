# 0071. A task's initial state reaches the runner by reference

- **Status:** Draft (not proposed yet)
- **Date:** 2026-11-05
- **Deciders:** — (drafted by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

ADR-0068 took a task's files off the registration message, ADR-0069 took the
trial's final state off `GetState`, and ADR-0070 removed the redundant copies
grading made of a large world. One copy of the world is left that every one of
them names as a follow-up: the **initial state**. The native adapter parses the
task's `initial_state.json_db` file into `TaskDescription.initial_state.tables`,
and that parsed world travels inline and stays resident on both ends for the
whole trial:

- **On the engine host**, the cached description holds the parsed tables, and
  the trial's setup holds three more parsed copies of the same file: the
  conductor's `EnvironmentState` reads it into `initial_db_state` and deep-copies
  it into `db_state`, and the adapter's `create_environment` parses it again into
  `AdapterEnvironment.data`. None of them is read for a task that provisions a
  database: its final state is the runner's. Registration then serialises the
  description with the tables in it (the `TrialSpec` JSON, the protobuf message
  and its bytes), as ADR-0068 described for tool artefacts.
- **On the runner**, the validated `TrialSpec` keeps the tables for the whole
  trial (`trial_context.task_description.initial_state.tables`), the DB client
  serialises them again into the `POST /trials/{id}/init` body, and `ToolFactory`
  keeps a reference. They are read again only at grading — the judge's state diff,
  `context.initial_state` of custom checks, a golden replay's reset, a comparison
  view — and at a `ResetTrial`.

The measured native-format task's state file is about 490 MB of JSON (ADR-0070).
Parsed, a JSON world costs several times its size: on a synthetic state of 40
tables of mixed-scalar records (47 MiB of JSON), parsing it and normalising it to
tables grows a process by +272 MiB at the peak, and the parsed tables alone stay
at about 4.6× the file. ADR-0070 found the runner's resident initial tables to be
about a third of its memory at rest.

## Decision Drivers

- Neither the engine host nor the runner holds a task's initial tables for the
  length of a trial; the runner reads them where they are used and drops them.
- Every grader sees the same tables, the same values and the same order as the
  inline delivery gives it.
- The runner uses exactly the bytes the task was described with, or refuses.
- Opt-in, on the footing of ADR-0068 and ADR-0069; a task that sets nothing is
  described, registered and graded byte-for-byte as before.
- No new service; what changes in the database service is additive and an older
  image keeps working.

## Considered Options

1. **Keep the tables inline, drop copies on each end** (release the host's
   setup copies, let the runner drop the tables once the database is seeded).
   Removes the host's three setup copies, but the description still carries the
   world, so the registration chain of ADR-0068 still holds several whole copies,
   and grading needs the tables back from somewhere.
2. **Read the initial state back from the database service.** The service keeps
   `initial_data` for `reset`, but no route returns it, and the sync of ADR-0070
   and golden replays mutate and restore the trial's state around it; a new
   route would move the same bytes over HTTP and add a read nothing else needs.
3. **The initial state as a referenced tool artefact** (this decision). The task's
   state file is already a file under the task directory that ADR-0068 mounts and
   verifies; the description names it instead of carrying its rows.

## Decision

Adopt **Option 3**, opt-in through the task's environment.

- **Declaration.** `environment_manifest.initial_state_delivery: inline | mount`
  (default `inline`), on `EnvironmentPatch` and `EnvironmentManifest`, a
  policy-request field that survives a task's atomic `stack` replacement. The
  runner reads the file from the task directory ADR-0068 mounts, so the manifest
  refuses `mount` unless `tool_artifacts_delivery` is `mount` too, and ADR-0068's
  scope rule (a `trial`-scope stack owns the runner) applies through it. Only a
  `json_db` that names a file is delivered by reference; one written inline in
  `task.yaml`, or none, travels inline whatever the field says.
- **Wire.** `RunnerInitialStateConfig.tables_ref: InitialStateTablesRef | None`
  with `artifact` (a key of `tool_artifact_refs.files`), `sha256` and `size`
  (that entry's); `tables` is then empty. The description refuses a `tables_ref`
  naming no referenced artefact or another digest, and tables both inline and by
  reference. `tables_ref` is left out of the dump while absent.
  `provisions_database` counts a reference without reading it.
- **Adapter.** The native adapter reads the delivery off the two environment
  patches as the resolver merges a policy field, so a task that sets nothing is
  answered without resolving anything, and checks the resolved manifest agrees.
  Under `mount` it never parses the file: the state file is named by the artefact
  that already carries it (the task directory's data files, or the case state
  copied beside a shared MCP server), or added under its path in the task
  directory, and `tables_ref` repeats that artefact's digest. The checks that read
  rows (`state_checks.id_fields`, a comparison view, the authored view in
  `get_grading_config`) move to `RegisterTrial`. `create_environment` leaves
  `data` empty, and the grading gate's seeded-tables layer reads the file only if
  a rule asks for the tables (a pack declaring neither `id_fields`, a view, a
  database-reading state check nor a JSON-DB builtin is gated without it). The
  normalisation from `json_db` to tables is one function,
  `runner/initial_state_file.seeded_tables`, which both deliveries apply.
- **Engine host.** The conductor hydrates the trial's `EnvironmentState` without
  the DB seed for such a trial, and does not replay `initialization_actions` on
  the host: that replay only shaped the host's copy of the DB state, which a task
  that provisions a database overwrites with the runner's final state.
- **Runner, registration.** The state file arrives with the other references,
  copied into the trial's artefact directory and verified (ADR-0068). The trial
  context keeps an `InitialStateFile` — the copy's path, SHA-256 and size — and no
  tables. The database service is seeded by streaming the file's bytes, unparsed,
  inside a JSON body to a new route, `POST /trials/{id}/init/json_db`, which
  normalises each collection as the engine does and initialises the trial as
  `/init` does. The client asks the route first with a body it must refuse, so a
  database service that predates it (FastAPI's bare 404) is seeded inline from
  the file read once instead. The tables are parsed at registration only when
  something there reads rows — `id_fields`, a comparison view (checked with the
  shapes the file was written in, which the inline wire cannot carry), an app
  world (ADR-0062), `mcp_async` toolset models — and those are dropped when
  registration returns; `ToolFactory` keeps the first row of each table, which is
  all its model matching reads. The JSON-DB builtin rule is asked with the table
  names the seeding answered.
- **Runner, grading.** `TrialContextRuntime.initial_tables()` reads the file on
  each call, checking its bytes against the digest and normalising it; inside a
  `GradeTrial` (`holding_initial_tables`) the first read is kept until the grade
  returns. The grading substrate takes it as an `initial_state_factory`, so a grade
  whose components never read the initial state never parses the file. Custom
  checks read it through the substrate; a golden replay's reset, a comparison view,
  `ResetTrial` and `SubstrateService.ReadInitialState` through `initial_tables()`.
  A file changed after registration is refused at every read.
- **Graders off the runner.** The state file is a referenced artefact, so
  ADR-0068's refusals already cover the topologies that cannot read it:
  `grader_rpc`, `queue` and snapshot bundles at run start, and the detached
  grader, the offline composite kind and the bundle producer as a second line.
- **Protocol.** `ENGINE_PROTOCOL_VERSION` becomes 10. An older image forbids
  `initial_state_delivery` and `tables_ref` and refuses every trial spec at
  `Invalid trial_spec_json` (an image that ignored them would seed an empty
  database); an older engine is refused by the runner's version gate.

## Consequences

### Positive

- The engine host holds no parsed copy of the initial state for such a task: the
  cached description, `EnvironmentState`'s two copies and `create_environment`'s
  are gone, and registration serialises a description of paths and digests.
- The runner holds no initial tables during the episode. Seeding streams the file
  (synthetic state above: +0 MiB against +321 MiB to parse it and build the inline
  body), and a grade that reads the initial state parses it once (+272 MiB on the
  synthetic state) and drops it when the grade returns.
- A pack whose state file is already one of its artefacts (the case-pack layout:
  `initial_state.json` beside its server) is copied once per trial, as before.

### Negative / Trade-offs

- A grade that reads the initial state parses the file again, a few seconds for a
  world of hundreds of MB, and holds it for the grade: grading's peak does not
  fall by the initial tables, its resting memory does.
- The registration checks that read rows parse the file at registration (once),
  so a pack declaring `id_fields` or a comparison view still pays a transient
  parse there.
- A state file holding a collection that is not a list of records is refused at
  registration (the database service's `422`), not when the description is built.
- A task that delivers its state by reference provisions a database even when the
  file is `{}`; inline, such a task provisions none.
- The grading gate's classmethods see the task's own opt-in, not a project-wide
  one; a pack opted in by the project's `default_environment` alone is gated with
  the file read once, as an inline pack is.
- Non-finite numbers (`NaN`, `Infinity`), which JSON does not define, reach the
  database service as written by reference; inline, the description's serialiser
  writes them as `null`.
- Protocol 10 requires rebuilding or re-pinning runner images, as 8 and 9 did.

### Follow-ups

- The database service still holds three copies of the world (`InitRequest`,
  `data`, `initial_data`), whichever delivery seeds it.
- A grade could take the initial state from the database service's own
  `initial_data` instead of parsing the file again, if a route served it.
- A server that keeps a digest of its initial state (ADR-0070's measured world)
  could hand the runner that digest, so a judge's diff would need no initial
  tables at all.
- Documentation updated: `TASK_DESCRIPTION_SCHEMA.md`, `GRPC_PROTOCOL.md`,
  `MULTI_CONTAINER_GUIDE.md`, `NATIVE_ADAPTER.md`, `DB_SERVICE_API.md`.
- Tests added (`tests/unit/runner/test_initial_state_by_reference.py`): the wire
  model and its refusals, the resolver's pairing rule, the adapter's reference
  (the artefact it reuses or adds, the file never parsed on the host, inline
  unchanged, row checks moved to the runner, a file outside the task directory),
  the lazy gate layer, the conductor's setup, the compose mount, the file's
  verification, the vendored normaliser's parity, the streamed route in process
  and over HTTP, the fallback to an older service, `RegisterTrial` with and
  without row checks, the JSON-DB rule, the lazy and held grading read, custom
  checks' equal view, and the protocol gate. A run against a real Docker stack is
  not part of the unit suite.

## Links

- Related ADRs: [0068](0068-tool-artifacts-by-reference.md) (the task's files by
  reference), [0069](0069-final-state-by-reference.md) (the final state by
  reference), [0070](0070-grading-holds-a-large-world-once.md) (grading holds a
  large world once), [0062](0062-app-world-served-over-http.md) (app worlds).
- Related code: `runner/initial_state_file.py`, `runner/models.py`
  (`InitialStateTablesRef`), `adapters/native.py`, `core/conductor.py`,
  `core/env_state.py`, `runner/service.py` (`_prepare_initial_state_file`,
  `_init_trial_db`, `TrialContextRuntime.initial_tables`), `runner/db_client.py`
  (`init_trial_from_json_db`), `env/json_db_service/app.py`
  (`init_trial_from_json_db`).
