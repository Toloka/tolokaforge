# 0070. Grading reads a large world one table at a time, from one snapshot

- **Status:** Draft (not proposed yet)
- **Date:** 2026-11-05
- **Deciders:** — (drafted by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

ADR-0068 moved a task's files off the registration message, and ADR-0069 moved
the trial's final state off `GetState`. With both, the measured task (a
native-format task whose world is about 6.5k files and whose database state is
an initial state of about 490 MB as JSON) runs end to end in a 24 GiB sandbox.
The trial's memory peak has moved to grading, inside the runner.

Measured on that trial, sampled once a second, with both earlier ADRs applied:

| Process | During the episode | Peak, at grading |
|---|---|---|
| Runner | 2.7 GiB | 8.9 GiB |
| Task's MCP server (the state holder) | 4.0 GiB | 4.7 GiB |
| Database service (the mirror) | 2.2 GiB | 2.6 GiB |
| Engine host | 3.1 GiB | 3.1 GiB |
| Sandbox, anonymous memory | about 12 GiB | 18.8 GiB of 24 |

The runner does not reload the world from a file at grading. It walks the world
around a loop, and each step holds its own whole copy:

1. **The initial state, for the whole trial.** `TaskDescription.initial_state.tables`
   still travels inline (ADR-0068 left it there). The runner keeps the parsed
   tables for the trial's lifetime: the substrate's `initial_state()` returns
   them, the judge's state diff and hash grading read them. This is most of the
   runner's 2.7 GiB at rest. The same tables went to the database service once,
   in `RegisterTrial`'s single `POST /trials/{id}/init`.
2. **The mirror sync.** A trial whose tools run in an MCP subprocess keeps its
   state there; the database service only mirrors it. Each grading call first
   runs `_sync_live_state`: the runner reads the subprocess's whole held state
   (one JSON document over stdio), then `_sync_mcp_state_to_db` reads the
   mirror's whole current state (`get_state`, one HTTP response), compares them
   table by table, and sends the mutations. Two whole worlds are alive while
   they are compared, even when the agent changed a handful of rows.
3. **Two final views.** The substrate's factories read the final state twice,
   whole: RAW (`get_state`) for the judge's state diff and `custom_checks`, and
   STABLE (`get_stable_state`, unstable fields filtered by the service) for
   JSONPath assertions.
4. **The diff and the checks.** The judge builds its state diff from the whole
   initial and the whole final state, and `custom_checks` receive the whole RAW
   final state as a `dict`. (An agentic judge's DB tools already go to the
   service per request on this path, through the `DBReader` seam: `get_state`
   with a table list and a server-side `query`.)

Each copy is a Python object graph several times the JSON's size, and the HTTP
and stdio bodies add their own bytes and strings while they are parsed. The
world is mostly static: the agent's writes touch a few tables, while every step
above handles all of them.

## Decision Drivers

- The runner's memory at grading does not grow with the number of whole-world
  copies; it is bounded by the tables a grader actually reads, plus one table
  in flight.
- What every grader sees is unchanged: the same rows, the same RAW and STABLE
  semantics, the same diff, the same verdict, byte for byte where it is written.
- A task that sets nothing grades exactly as before; the change is opt-in at
  first, on the same footing as ADR-0068 and ADR-0069.
- The `custom_checks` contract keeps working for an existing `checks.py`: a
  check that reads `final_state["orders"]` or iterates every table still does.
- Each step can land and be measured on its own.

## Considered Options

1. **A larger sandbox.** 32 GiB fits today's peak with room, and is what a
   measured run would need tomorrow for the next larger world. It removes no
   copy.
2. **Faster parsing** (`orjson`, `ijson`, `msgspec`). A constant factor on the
   parse time and a little on the object size; every copy stays.
3. **Drop the mirror and grade from the holder.** Ask the MCP subprocess for
   tables as graders need them, and retire the database service for such tasks.
   The mirror also answers JSONPath queries server-side and holds the
   unstable-field specs, so this is a redesign of the state plane, not a change
   to grading.
4. **One snapshot, read one table at a time** (this decision). Sync the mirror
   by table digests instead of whole states, read the final state once, derive
   the STABLE view from it in the runner, hand graders read-only mappings that
   load a table on first access, compute the judge's diff table by table, and
   keep the initial state as a file rather than a resident object.

## Decision

Adopt **Option 4**, in four steps that each stand alone. The first needs no
protocol change; the fourth is opt-in under `tool_artifacts_delivery: mount`.

1. **One RAW read; STABLE derived in the runner.** The substrate reads the final
   state from the mirror once, and computes the STABLE view by applying the
   trial's `unstable_fields` specs to the RAW rows, exactly as the service does
   server-side (the same function, moved or shared). The second whole-state
   HTTP read and its object graph go away. A cross-check test runs both paths on
   the same state and requires equal results.
2. **Mirror sync by table digests.** The MCP state helper answers a digest per
   table (`_tolokaforge_get_state_` with a `digests` request), and the database
   service answers its own per-table digests (`GET /trials/{id}/digests`), both
   over the same canonical JSON encoding of a table's rows. The runner fetches
   and compares only the tables whose digests differ, one table at a time
   (`tables=[name]` on both sides), and mutates those. An unchanged world syncs
   without either whole state being read.
3. **Table-at-a-time views for graders.** `initial_state()`, `final_state()` and
   `final_state_stable()` return a read-only `Mapping[str, list]` that loads a
   table on first access and keeps at most a bounded number of tables resident
   (least recently used). Iteration, `in`, `len` and `keys()` answer from the
   table list without loading rows. The judge's state diff is computed one
   table at a time, skipping tables whose initial and final digests are equal.
   `custom_checks` receive the same
   mapping; a check that calls `dict(final_state)` or mutates it gets a whole
   copy or an error, respectively, which the refusal below makes explicit.
4. **The initial state by reference** (opt-in, under `tool_artifacts_delivery:
   mount`). The adapter records `initial_state.tables` as a reference to the
   pack's state file (`{source, sha256, size}`, as ADR-0068's `tool_artifact_refs`)
   instead of inline. The runner verifies the file on the mount, streams it into
   the service's init request (a chunked upload, never parsed whole), and serves
   `initial_state()` from it one table at a time. This removes the runner's
   resident copy and the last part of the world that crosses the wire inline.
   Wire protocol bump; old and new pairings refused at registration.

`custom_checks` that need the whole state at once keep working: the mapping
loads every table when a check walks them all, so such a check pays what it pays
today, and the bounded residency then spills to reloading. A check that mutates
the state it is handed is refused (the mapping is read-only), which today would
corrupt the judge's view silently.

## Consequences

### Positive

- The runner's grading peak no longer holds the world five times; for the
  measured task the estimate is the largest table plus the changed tables, a
  fraction of today's 8.9 GiB.
- The mirror sync of a mostly static world reads two short digest lists instead
  of two whole states.
- Step 4 removes the runner's resident initial state, its largest single object
  between registration and grading.

### Negative / Trade-offs

- The database service gains a digest endpoint and the MCP state helper a
  digest request, so packs that ship their own MCP server must update the helper
  to benefit from step 2; without it the sync falls back to today's whole read.
- A digest over a table's canonical JSON costs a pass over that table on both
  sides at every sync, which is cheaper than shipping it but not free.
- Deriving STABLE in the runner makes the unstable-field rules a shared function
  of two processes; a divergence would change verdicts, which the cross-check
  test guards.
- A `checks.py` that relied on receiving a mutable `dict` breaks, deliberately.

### Follow-ups

- Measure each step on the large-world trial in the 24 GiB sandbox; record the
  runner's peak after each.
- The task's MCP server still holds the whole world (4.7 GiB): that is the
  world's working set and out of scope here.
- The engine copies a pack's whole compose directory into every trial's context
  (ADR-0068 follow-up); unrelated to grading but next on the disk side.

## Open questions

- Does any shipped `checks.py` mutate `final_state` or require `isinstance(..., dict)`?
  Grep the public examples and add a compatibility note.
- Is the service's server-side STABLE filter a pure function of the rows and the
  `unstable_fields` specs, or does it depend on service state (versions, write
  history)? Step 1 assumes the former.
- Which canonical encoding for table digests: the service already computes a
  stable hash for the whole state; per-table digests should reuse its encoding.

## Links

- Related ADRs: [0068](0068-tool-artifacts-by-reference.md) (task files by
  reference), [0069](0069-final-state-by-reference.md) (final state by
  reference).
- Related code: `runner/service.py` (`_grade_trial_async`,
  `_build_grading_substrate`, `_sync_live_state`, `_sync_mcp_state_to_db`),
  `runner/db_client.py`, `core/grading/substrate.py`,
  `core/grading/composite/llm_judge.py`, `core/grading/composite/custom_checks.py`,
  `core/grading/default_state_check_backends.py`.
