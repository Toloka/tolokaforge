# 0070. Grading holds a large world once

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
The trial's memory peak has moved to grading, inside the runner: 8.9 GiB, with
the sandbox's anonymous memory at 18.8 GiB of 24.

The world's shape matters. Its database state is **one table holding one row**,
`{id, state}`, whose `state` field nests every application's tables (about
300 MB of canonical JSON). Any scheme that reads or diffs "a table at a time"
handles the whole world in its first step.

Sampled once a second, the runner's memory during `GradeTrial` breaks down as:

| When | Runner | What it holds beyond its 3 GiB at rest |
|---|---|---|
| The sync before grading | 5.7 GiB | The holder's whole state (stdio) and the mirror's whole state (HTTP), compared table by table |
| The judge's state diff | **8.9 GiB** | The RAW final state read back from the mirror, and the diff of one field: `json.dumps` of its old and of its new value, each the whole world, joined into one fragment |
| Custom checks | 4.2 GiB | The RAW final state, memoised |

Three of those copies are redundant:

1. **The sync re-reads a state it just wrote.** `GetState` syncs the mirror,
   and `GradeTrial` syncs it again a minute later on the same state; each sync
   reads the mirror's whole state to compare it with the holder's.
2. **The final state is read back after the sync.** The sync held the holder's
   state, which the mirror then equals, and dropped it; the grading factories
   read the same state back from the mirror over HTTP.
3. **The judge's diff serialises what it cannot show.** `render_state_diff`
   formats a changed field's old and new values whole before cutting the diff
   to its 50k-character cap. For a one-row world the judge receives
   `runtime_evidence: 1 modified … (state diff truncated)`: gigabytes of
   strings to say nothing.

## Decision Drivers

- The runner's grading peak does not grow with redundant whole-world copies.
- What every grader sees is unchanged where it was useful, and becomes useful
  where it was not (the judge's diff of a large field).
- A db-service that predates the change keeps working.
- No wire protocol change; each step lands and is measured alone.

## Considered Options

1. **A larger sandbox.** Fits today's peak; removes no copy.
2. **Faster parsing** (`orjson`, `msgspec`). A constant factor; every copy stays.
3. **Table-at-a-time grading** (this ADR's first draft): sync by table digests,
   lazy read-only table mappings, a table-by-table diff, the initial state by
   reference. Sound for many-table worlds, but the measured world is one table
   of one row, so it removes none of the measured copies by itself.
4. **Hold the world once** (this decision): the diff never serialises past what
   it shows, the sync reads back only tables whose digests differ, and the
   final state is taken from the synced holder copy wherever the mirror's
   digest vouches for it.

## Decision

Adopt **Option 4**, in two steps.

1. **The judge's state diff descends into a field too large to show.** A
   changed field whose JSON would run past the diff's cap is diffed path by
   path (nested dicts field by field, large lists by a key the rows carry or by
   position), each value cut to 2k characters, at most 50 paths per field. No
   value is encoded past the size it is shown at (`JSONEncoder.iterencode`,
   abandoned at the limit); keyless tables count records by digest rather than
   by their JSON. Everything that fits renders exactly as before. The judge
   now sees, for the one-row world,
   `state.apps.jira.issues.12649.assignee: null → "…"`.
2. **The mirror sync by table digests; the final state from the synced copy.**
   The db-service answers `GET /trials/{id}/state/digests`: each table's
   SHA-256 of its canonical JSON (`sort_keys`, compact separators, ASCII),
   encoded a row at a time, in the state's own table order. The db-service
   image runs a vendored copy held to core's by a parity test. The sync
   digests the held tables, reads back from the mirror only the tables whose
   digests differ, one at a time, mutates them, and re-digests them. A table
   whose mirror and held digests then agree is byte-identical JSON, so the
   grading's RAW final state takes it from the held copy and reads only the
   rest, in the mirror's table order. A second sync of the same state reads no
   rows. A db-service without the route (FastAPI's bare 404 or 405) is synced
   and read whole, as before.

A digest mismatch is always safe: it only makes the runner read the mirror's
table instead of trusting its own copy (an int the holder keeps as a float is
equal in Python, never mutated, and so read back).

## Consequences

### Positive

- The judge's diff of the measured world costs 0.5 s and 70 MiB instead of
  8.8 s and 0.8 GiB (locally), and shows what the agent changed.
- `GradeTrial`'s sync of an unchanged world reads two digest lists, and its
  final state is the holder's copy: the second and third whole-world copies go.
- No protocol change; an older db-service degrades to today's behaviour.

### Negative / Trade-offs

- Each sync digests the held state (one pass of `json.dumps` per row) and asks
  the mirror to digest its tables: CPU on both sides, a few seconds for the
  measured world, against reading and parsing it whole.
- The RAW final state handed to graders is the holder's object graph rather
  than a fresh parse of the mirror's JSON. A grader that mutated it would have
  mutated a memoised copy before too; nothing reads the held copy after
  grading.
- The diff's output changes for a field longer than the cap, from a truncation
  flag to per-path lines.

### Follow-ups

- **The initial state by reference** (this ADR's first draft, step 4): the
  runner keeps `TaskDescription.initial_state.tables` resident for the trial,
  about a third of its memory at rest, and the engine host holds it too.
- **The first sync's mirror read**: the sync after the episode still reads the
  changed table whole to compute its ops; for a one-row world that is the whole
  world. Row digests, or a replace-table op, would remove it.
- **STABLE derived in the runner** (first draft, step 1), for tasks with
  JSONPath checks; the measured task reads no STABLE view.
- The 77–78 s pause before the MCP server begins to answer the `GradeTrial`
  sync, seen on both measured trials with every process's memory flat; it
  needs a CPU sample to place.

## Measured result

The large-world trial in the 24 GiB sandbox, same task, model and engine stack
(ADR-0068, ADR-0069 and the DB init budget), before and after both steps; peak
resident memory per process in each phase, sampled once a second:

| Phase | Runner before | Runner after | Sandbox before | Sandbox after |
|---|---|---|---|---|
| `GetState` (first sync, final state file) | 5.6 GiB | 5.1 GiB | 19.2 GiB | 19.4 GiB |
| `GradeTrial` sync | 5.5 GiB | 4.1 GiB | 19.2 GiB | 18.1 GiB |
| Judge | **8.7 GiB** | 3.8 GiB | **22.2 GiB** | 17.8 GiB |
| Custom checks | 3.9 GiB | 3.4 GiB | 17.2 GiB | 17.6 GiB |

The trial's peak moved from the judge to `GetState`'s first sync, whose
changed table is still read back whole (see Follow-ups). The judge's diff went
from a truncation flag to the agent's edits by path: new Jira comments and
issues, Slack messages, the cancellation of one order line, a grant schedule's
revision. The `GradeTrial` sync still spends 78 s before the holder starts to
answer, as it did before the change; that pause is not in the code this ADR
touches.

## Links

- Related ADRs: [0068](0068-tool-artifacts-by-reference.md) (task files by
  reference), [0069](0069-final-state-by-reference.md) (final state by
  reference).
- Related code: `core/grading/state_diff.py`, `core/hash.py` (`table_digest`),
  `env/json_db_service/app.py` (`get_state_digests`), `runner/db_client.py`
  (`get_table_digests`), `runner/service.py` (`_sync_mcp_state_to_db`,
  `_HeldStateSync`, `_mirror_state_after`, `_build_grading_substrate`).
