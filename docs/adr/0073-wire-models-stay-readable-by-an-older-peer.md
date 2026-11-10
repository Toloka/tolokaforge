# 0073. A wire model stays readable by an older peer until the protocol says otherwise

- **Status:** Proposed
- **Date:** 2026-11-10
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine talks to the runner and the grader over gRPC, but most of what it sends is
not proto: `trial_spec_json`, `task_description_json`, `task_config_json`,
`judge_model_config_json` and `bundle_manifest_json` are pydantic models dumped to JSON
and carried in a string field. The two halves behave in opposite ways. A proto field a
peer does not know is dropped silently (proto3). A JSON key a peer does not know is
refused outright, because every wire model is `extra="forbid"`. Adding an optional field
to such a model is therefore a breaking change for every older peer that reads it, even
when the new field holds its default and means nothing to that peer.

Two mechanisms exist today, and neither covers this:

- **The protocol version** (`ENGINE_PROTOCOL_VERSION`, [GRPC_PROTOCOL.md](../GRPC_PROTOCOL.md)
  § Version lock) gates *semantics*. The runner refuses an engine below its own version
  at `RegisterTrial`, and the host refuses a runner image below its own. The grader wire
  has no such gate: `GradeRequest` carries no protocol version, and a `grader_rpc` or
  `queue` grader parses `task_description_json` with no handshake.
- **The wire census** (`tests/canonical/test_grading_wire_lock.py`) locks every key the
  engine emits under `TaskDescription.grading` and `.search`, the two roots it was written
  for (`_CENSUS_ROOTS`). The description's top-level fields, `TrialSpec`'s own fields and
  the grader's other JSON payloads are outside it.

Some models already keep themselves readable by leaving a field out of their dump while it
holds its default: `SearchConfig.OMITTED_AT_DEFAULT`, `comparison_view`, `mutates_state`,
`app_world` and `tables_ref` through `omitted_when_absent` and
`core/grading/omitted_fields.py`. It is a convention applied field by field, by memory.

**The incident.** ADR-0068 and ADR-0069 added `TaskDescription.trial_data_delivery`
(default `inline`) and `tool_artifact_refs` (default `None`). Every description, including
an inline one that uses neither, then dumped both keys. The 0.34.7 grader image refused
every `Grade` (`extra_forbidden`), and an older runner image would have refused every
`RegisterTrial` for a task it serves unchanged. The census did not see it, because both fields
sit outside its two roots. The runner pairing was gated by protocol 8 and 9 anyway, but the
grader pairing had no gate. The only test to catch it was the published-image lane of
`test_stack_grader_composite_dispatch_grades_transcript_rules`, after the merge to `main`.
The fix (#1997) applied the same omit-at-default convention to the two fields.

The question this ADR answers is open question 3 of ADR-0072: how the wire treats keys a
peer does not know.

## Decision Drivers

- A field added to a wire model must not break an older peer that never needs it. An
  inline trial graded by an older grader image is the case to protect.
- A field an older peer *must* understand must still fail loudly and before tokens are
  spent. Silently dropping it is the failure mode protocol 6 exists to prevent: a
  `RunTestSuiteRequest` field an older image dropped zeroed most of a benchmark.
- The rule must be enforced by a test that sees every JSON wire root, not by review
  memory.
- Strict parsing stays: `extra="forbid"` catches typos in authored packs and in
  hand-written payloads.

## Considered Options

1. **Omit at default, enforced.** Wire models stay `extra="forbid"`. A new optional field
   is left out of the dump while it holds its default (`omitted_when_absent` /
   `OMITTED_AT_DEFAULT`). A field that must reach the peer whenever it is set either keeps
   that rule (its presence alone tells the older peer it cannot serve the payload) or
   comes with a protocol bump. A canonical test makes the rule non-optional for every
   JSON wire root.
2. **Tolerant readers with a must-understand list.** Wire models become `extra="ignore"`
   on the reading side, and each model declares the fields a peer must refuse when it
   does not know them. This is closer to proto3, but strictness then depends on a list a
   new field can forget, and the reader can no longer tell a typo from a future field.
3. **Version-gated dumps.** The writer learns the peer's protocol (the runner's from
   `RegisterTrialResponse`, the grader's from a new field) and dumps only what that
   version declares. It is precise, but it needs a per-version schema registry on the
   writing side and a grader handshake that does not exist yet.

## Decision

We will adopt **Option 1**, and make it a rule a test enforces rather than a convention.

- **The rule.** A field added to a model reachable from a JSON wire root has a default and
  is left out of the dump at that default. `leave_out_absent_fields` covers `None`, and
  `OMITTED_AT_DEFAULT` covers any other default, such as `trial_data_delivery: inline`.
  A payload that uses the field carries it, and an older peer refuses that payload by
  name. This is the right outcome: the peer could not serve it. When an older peer must
  be refused even for the default, the field instead comes with a protocol bump and its
  row in GRPC_PROTOCOL.md § Version lock.
- **The roots.** The JSON wire roots are the models dumped into a proto string field:
  `TrialSpec` (which embeds `TaskDescription`), `RunnerGradingConfig` and the judge's
  `ModelConfig`, and any later one (`bundle_manifest_json`, still unpopulated, would be
  the next). They are listed once, in a test module,
  with the proto field each one rides.
- **The test.** For each root, a canonical test dumps a minimal valid instance (every
  optional field at its default), records its key paths, and compares them with a frozen
  key set committed under `tests/canonical/snapshots/wire_floor/`. The frozen set is the
  keys the oldest supported image declares (`_SUPPORTED_IMAGE_FLOOR`), regenerated from
  that release's models by a script when the floor moves. A default dump that gains a
  key fails the test and names the path. The way out is omission at the default, or
  moving the floor in the same PR as a protocol bump. The grading census keeps its own,
  deeper lock on `grading` and `search`. The new test covers every key at and above
  them, and the census's `omitted_when_absent` handling is reused, not duplicated.
- **The grader.** `GradeRequest` gains `engine_protocol_version`, and the grader service
  refuses an engine above the highest version it knows with a named skew instead of
  `extra_forbidden`. This is the grader half of the gate the runner already has, and
  it makes the published-image lane a second line rather than the only one.

Option 2 stays available for a later ADR, should the engine need a peer that reads
payloads from several newer writers. Option 3 is not needed while Option 1 holds.

## Consequences

### Positive

- An inline trial and its grade keep working across an engine upgrade with an older
  runner or grader image wherever the protocol does not forbid the pairing. The 0.34.8
  incident becomes a red unit test on the PR that adds the field.
- The rule is one sentence, already practised across the wire models, with one helper and
  one test.
- An older peer still refuses a payload it cannot serve, by the name of the key, before
  any tokens are spent.

### Negative / Trade-offs

- A dump no longer shows every field: a reader of `env.yaml` or a snapshot must know that
  an absent key means the default. The models already read it that way.
- The frozen key set must be regenerated whenever the support floor moves. That is one
  script run in the PR that moves it, but it is a step a reviewer can miss. The test
  names the script in its failure.
- The grader gate adds a proto field and a refusal path to `grader_rpc` and `queue`.

### Follow-ups

- Code changes required: the wire-root test and its frozen key sets with their generator
  script; `GradeRequest.engine_protocol_version` and the grader's refusal; any existing
  root field the test finds emitted at its default.
- Documentation to update: GRPC_PROTOCOL.md § Version lock (the rule, and the grader's
  version), TASK_DESCRIPTION_SCHEMA.md (a dump leaves defaults out), CONTRIBUTING.md
  (adding a wire field).
- Tests to add: the wire-root test; a grader test for the version refusal.

## Links

- Related ADRs: [0068](0068-tool-artifacts-by-reference.md), [0069](0069-final-state-by-reference.md),
  0071 (initial state by reference, `tables_ref`), 0072 (the trial data plane, open
  question 3).
- Related code: `core/grading/omitted_fields.py`, `runner/models.py` (`TaskDescription`,
  `SearchConfig`), `runner/protocol.py` (`ENGINE_PROTOCOL_VERSION`), `grader/wire_snapshot.py`,
  `grader/composite_dispatch.py`, `tests/canonical/test_grading_wire_lock.py`.
- External references: #1997 (the incident's fix).
