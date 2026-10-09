# 0056. Shared tool libraries reach the trial through `tool_artifacts`, pinned by the task and added by the engine

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tool code reaches a trial only as base64 `tool_artifacts`. The runner extracts
them into the trial's directory and puts that directory on `sys.path`
(`runner/service.py`). The grader extracts the same set for `custom_checks`
(`core/grading/tool_artifacts.py`). The mechanism does not depend on the
adapter, but each adapter decides what goes into `tool_artifacts`. The native
adapter bundles the task directory, or the domain root with `_shared/`. An
external adapter bundles its own host package and the case.

No adapter can add a library installed next to the engine. Packs that share
tool code therefore carry byte-identical copies of it, and copies of the same
service mock in different packs drift apart. A shared library of
vendor-shaped service mocks, maintained once and reused across packs and
benchmarks, would have to be copied into every pack the same way.

A pack can already use such a library without any engine change: it vendors a
generated copy of the library's bundle (Option 1 below). This ADR is about
removing that copy, not about making the library usable at all. It is
independent of ADR-0057 and ADR-0058.

## Decision Drivers

- One maintained implementation per service, pinned by version, for every adapter.
- The runner and host-side grading run the same library revision.
- No pip and no network at trial time. The runner image does not change per library.
- A pack declares what it needs in one line and drops its copy.

## Considered Options

1. **Generated copy in the pack.** A sync script writes the library's bundle
   into the pack, and a pack test checks the copy against the pin. Works today,
   but every pack still carries a copy and the copies drift.
2. **Engine hook.** A pack declares `tool_libraries: [{name, version, apps}]`.
   The engine resolves the installed library and adds its bundle to
   `tool_artifacts` for any adapter.
3. **Library wheels in an environment-tier runner image**, with a pin check at
   `RegisterTrial`.
4. **Wheel installed into a per-trial venv** at `RegisterTrial`.

## Decision

We will adopt **Option 2**. Option 1 stays the bridge for packs until this
lands, with the same bundle format, so moving a pack deletes files and adds one
line.

- **Registration.** A new entry-point group `tolokaforge.tool_libraries`
  (registered like the other seams of ADR-0011) lets an installed library
  register an object with `name`, `version` and
  `bundle(apps) -> {relative path: bytes}`.
- **Declaration.** `TaskConfig` gains `tool_libraries: list[{name, version, apps?}]`.
  A shared domain config declares it for every case, the same way it declares
  tools.
- **Resolution.** One engine path produces the wire description for every
  caller: the orchestrator, `run_trial` and the dry run. It calls the adapter's
  `to_task_description`, then resolves each pin. It refuses a missing library,
  and a version mismatch with both versions in the message. It merges the
  bundle into `tool_artifacts` and refuses a path that the adapter's own
  artefacts already hold. It records provenance (name, version, applications,
  file count) under `TaskDescription.metadata["tool_libraries"]`. Adapters do
  not bundle libraries.
- **Import path.** The runner starts MCP server subprocesses with the trial's
  artefact root ahead of `PYTHONPATH`. A server under `_shared/` then imports a
  library rooted at the artefact directory, as the runner process does.
- **Dependencies.** A library imports only packages the runner already depends
  on (`pydantic`, `pyyaml`, …), because the bundle runs on the runner's
  interpreter like an adapter's own artefacts. A library's CI tests its bundle
  with only the engine installed.

Option 3 stays the documented fallback for a library with heavy dependencies.
Option 4 is rejected: it puts pip and venvs into the runner, and a separate venv
does not serve in-process tools.

## Consequences

### Positive

- Every adapter gets the library without adapter code. Packs drop their copies
  once they pin the library.
- The runner and the grader cannot drift: both run the bundle shipped with the trial.
- A version pin is checked when the task is described, before any container starts.

### Negative / Trade-offs

- Every trial still carries the bundle's sources. A library bundles only the
  selected applications to keep this small: a bundle of two mocked
  applications measured about 350 KiB.
- The runner's dependency set becomes a contract for installed libraries.
  Adding or removing a runner dependency can break a library.

### Follow-ups

- Code changes required: the entry-point group and resolver;
  `tool_libraries` in `TaskConfig` and the shared-domain config; the merge into
  `tool_artifacts` on the single description path; provenance; `PYTHONPATH` for
  MCP subprocesses; `tolokaforge validate` reporting unresolvable pins; external
  adapters that generate packs forwarding a domain's `tool_libraries`.
- Documentation to update: `PROJECTS.md`, `ADAPTER_INTERFACE.md`,
  `TASK_DESCRIPTION_SCHEMA.md`.
- Tests to add: a missing library and a version mismatch are refused; the
  bundle is merged for the native adapter and an external one; a path collision
  is refused; an MCP server under `_shared/` imports the library.

## Links

- Related ADRs: [0011](0011-seam-and-declaration-conventions.md) (entry-point registries), [0057](0057-mutates-state-on-the-tool-wire.md), [0058](0058-app-world-served-over-http.md).
- Related code: `runner/service.py` (artefact extraction), `core/grading/tool_artifacts.py`, `adapters/native.py` (`_bundle_task_artifacts`), `core/plugin_registry.py`.
