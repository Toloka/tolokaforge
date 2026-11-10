# 0073. A wire model stays readable by an older peer until the protocol says otherwise

- **Status:** Proposed
- **Date:** 2026-11-10
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

> Numbers 0071 and 0072 belong to two ADRs open on the branches of #1978 and #1983.
> This one takes the next free number, and supersedes neither.

## Context and Problem Statement

The engine talks to the runner and the grader over gRPC, but most of what it sends is
not proto: `trial_spec_json` (which embeds `TaskDescription`), the grader's
`task_description_json`, `task_config_json` and `judge_model_config_json` are pydantic
models dumped to JSON and carried in a string field. The two halves behave in opposite
ways. A proto field a peer does not know is dropped silently (proto3). A JSON key a peer
does not know is refused outright, because every wire model is `extra="forbid"`. Adding an
optional field to such a model is therefore a breaking change for every older peer that
reads it, even when the field holds its default and means nothing to that peer.

Two mechanisms exist today, and neither covers this:

- **The runner's protocol version** ([GRPC_PROTOCOL.md](../GRPC_PROTOCOL.md) § Version
  lock) gates semantics. The runner refuses an engine below its `ENGINE_PROTOCOL_VERSION`
  (`runner/service.py`), and the host refuses a runner image below its own
  (`core/shared_stack_runtime.py`), so the runner pair is effectively equal-version.
- **The grader has no protocol version.** `grader.proto` mirrors the runner's grade
  payload, but the two services rev on independent cadences
  ([ADR-0038](0038-grader-detachment.md)). `GradeRequest` carries no version, and a
  `grader_rpc` or `queue` grader parses `task_description_json` with no handshake.
- **The wire census** (`tests/canonical/test_grading_wire_lock.py`) locks every key the
  engine emits under `TaskDescription.grading` and `.search`. The rest of the trial spec
  is outside it and tracked in #983 (64 of 112 key paths uncensused).

Some models already keep themselves readable by leaving a field out of their dump at its
default. They do it in two ways:

- `omitted_when_absent` with the helper `leave_out_absent_fields` covers `None`:
  `mutates_state`, `app_world`, `comparison_view`, `regex_engine`, `tables_on_one_side`,
  `tool_artifact_refs`, `Grade.comparison_view` and `state_snapshots`.
- A per-class `OMITTED_AT_DEFAULT` ClassVar with a hand-copied loop covers any other
  default: `SearchConfig`, `JudgeEvidence`, `JudgeCustomization`.

`TaskDescription.trial_data_delivery` uses neither: a bespoke `pop` in
`_leave_out_an_inline_delivery`. The rule is applied field by field, by memory.

**The incident.** [ADR-0068](0068-tool-artifacts-by-reference.md) and
[ADR-0069](0069-final-state-by-reference.md) added `TaskDescription.trial_data_delivery`
(default `inline`) and `tool_artifact_refs` (default `None`). Every description, including
an inline one that uses neither, then dumped both keys. The 0.34.7 grader image refused
every `Grade` (`extra_forbidden`). The runner pairing was refused by protocols 8 and 9
anyway, but the grader pairing had no gate, and the census did not see the two keys,
which sit outside its roots. The only test to catch it was the published-image lane of
`test_stack_grader_composite_dispatch_grades_transcript_rules`, after the merge to `main`.
#1997 fixed it by leaving both keys out at their defaults.

This ADR answers open question 3 of [ADR-0072](https://github.com/Toloka/tolokaforge/pull/1983):
how the wire treats keys a peer does not know.

**Scope.** In scope: the JSON the engine sends to a runner or grader. Out of scope, each
with its own direction of compatibility:

- the reverse direction, a newer peer's JSON read by an older engine (`state_diff_json`,
  `details_json`, `comparison_view_json`, `state_snapshots_json`,
  `failure_evidence_json`, `GetState`'s `state_json`, `customization_json`);
- the trial-node wire (`trial_node_inputs_json`, `trial_result_json`), gated by its own
  `NODE_PROTOCOL_VERSION`;
- recorded files a later engine reads (`GradeBundleManifest`, `grade.yaml`, `env.yaml`).

## Decision Drivers

- A field added to a wire model must not break an older peer that never needs it. An
  inline trial graded by an older grader image is the case to protect.
- A field an older peer *must* understand must still fail loudly and before tokens are
  spent. Silently dropping it is the failure mode protocol 6 exists to prevent: a
  `RunTestSuiteRequest` field an older image dropped zeroed most of a benchmark.
- The grader keeps its own release cadence (ADR-0038).
- The rule must be enforced by a test that sees every engine-to-peer JSON root, not by
  review memory.
- Strict parsing stays: `extra="forbid"` catches typos in authored packs and payloads.

## Considered Options

1. **Omit at default, enforced, plus a grader protocol of its own.** Wire models stay
   `extra="forbid"`. A new field is left out of the dump at its default. A canonical test
   makes that non-optional for every root. The grader gets the version gate the runner
   already has, on its own counter.
2. **Tolerant readers with a must-understand list.** Wire models become `extra="ignore"`
   on the reading side, and each declares the fields a peer must refuse when it does not
   know them. Strictness then depends on a list a new field can forget, and a reader can no
   longer tell a typo from a future field.
3. **Version-gated dumps.** The writer learns the peer's protocol and dumps only what
   that version declares. It is precise, but it needs a schema registry per version on the
   writing side.

## Decision

We will adopt **Option 1**.

- **The rule.** A field added to a model reachable from an engine-to-peer JSON root has a
  default and is left out of the dump at that default: `leave_out_absent_fields` for
  `None`, and a shared `leave_out_default_fields` for any other default, read from the
  class's `OMITTED_AT_DEFAULT`. A payload that uses the field carries it, and an older peer
  refuses that payload by name. This is right, because the peer could not serve it. When
  an older peer must be refused even at the default, the field comes with a protocol bump
  instead.
- **Defaults are frozen.** Changing the default of an omitted field silently changes what
  an older peer assumes. Protocol 4's `regex_engine` is the precedent. So the change needs
  a protocol bump, and the test freezes each omitted field's (key, default) pair. A new
  `Literal` value is fine unless it becomes the default.
- **One floor per peer.** The runner pair is equal-version, so the runner's floor is the
  first release at the current `ENGINE_PROTOCOL_VERSION`, and it resets with every bump.
  The grader's floor is the first release at the grader protocol's current version. The
  census's `_SUPPORTED_IMAGE_FLOOR` (v0.13.1) is a different floor, for its documentation
  table, and stays as it is.
- **The test.** For each root and each producer of it (the native adapter's description
  through `TrialSpec.model_dump_json`, and `build_grade_request_fields`), a canonical test
  walks every model reachable from the root: nested models, union members, list and dict
  items. For each model path it records the keys a present instance emits at its defaults,
  plus the (key, default) pairs of the omitted fields. It dumps through the real producers
  because the omission serializers fall back to `handler(self)` for a mapping stored with
  `model_copy(update=…)`. The result is compared with a frozen set per peer, generated from
  the peer's floor release by a script that the failure names. A key a default dump gains,
  or a changed frozen default, fails and names the path. The way out is omission, or a
  protocol bump that moves that peer's floor in the same PR.
- **A grader protocol of its own.** `GRADER_PROTOCOL_VERSION` is a new counter, separate
  from `ENGINE_PROTOCOL_VERSION`, and moves only when grader-wire semantics change. It is
  gated like the runner pair:
  - The engine sends its value on `GradeRequest`, and a grader refuses an engine below its
    floor.
  - The grader reports its version on `HealthCheckResponse` and `GradeResponse`, and the
    engine refuses a grader below what the engine needs.
  - An older grader drops the new request field (proto3) and reports nothing, which reads
    as version 0. The engine needs version 0 until the first grader-wire semantic change,
    so the 0.34.7 grader keeps grading inline trials for a 0.34.8 engine.

Option 2 stays available for a later ADR, should a peer need to read payloads from several
newer writers. Option 3 is not needed while Option 1 holds.

## Consequences

### Positive

- An inline trial and its grade keep working across an engine upgrade with an older
  runner or grader image wherever the protocol does not forbid the pairing. The 0.34.8
  incident becomes a red test on the PR that adds the field.
- A grader skew the protocol does forbid is refused by name, before grading, instead of as
  `extra_forbidden`, and grader releases keep their own cadence.
- The default dumps of the whole trial spec are locked, which covers the first of #983's
  two surfaces for key presence. #983 stays open: the census's per-key gate and shape still
  cover `grading` and `search` only, and `trace_checks`' interior is untouched.
- GRPC_PROTOCOL.md's rows for protocols 8 and 9 become right again. Since #1997 an inline
  spec decodes on an older image, and the host names the skew through
  `runner_protocol_version` at registration, not through `Invalid trial_spec_json`.

### Negative / Trade-offs

- A dump no longer shows every field: a reader of a snapshot must know that an absent key
  means the default. The models already read it that way.
- The frozen sets are regenerated whenever a peer's floor moves. That is one script run in
  the PR that bumps the protocol, but a reviewer can miss it.
- A second protocol counter is a second number to keep straight. It moves rarely, but the
  version lock must be documented for each peer.

### Follow-ups

- Code changes required:
  - a `leave_out_default_fields` helper, with `SearchConfig`, `JudgeEvidence` and
    `JudgeCustomization` moved onto it;
  - `TaskDescription` declaring `OMITTED_AT_DEFAULT = {"trial_data_delivery"}` instead of
    its bespoke `pop`, so the test reads it as omitted rather than always emitted;
  - the wire-root test, its frozen sets and their generator;
  - `GRADER_PROTOCOL_VERSION`, carried on `GradeRequest`, `GradeJob` (`grader/queue.py`),
    `HealthCheckResponse` and `GradeResponse`, sent and checked by
    `GrpcGraderClient.grade`;
  - any existing root field the test finds emitted at its default.
- Documentation to update: GRPC_PROTOCOL.md § Version lock (the rule, rows 8 and 9, the
  grader's version), GRADER_SERVICE.md, TASK_DESCRIPTION_SCHEMA.md (a dump leaves defaults
  out), CONTRIBUTING.md (adding a wire field).
- Tests to add or update: the wire-root test; the `GradeRequest` shape lock in
  `tests/canonical/test_grader_wire_v2.py`; a grader test for each direction of the
  version refusal.

## Links

- Related ADRs: [0038](0038-grader-detachment.md),
  [0068](0068-tool-artifacts-by-reference.md), [0069](0069-final-state-by-reference.md),
  [0071](https://github.com/Toloka/tolokaforge/pull/1978),
  [0072](https://github.com/Toloka/tolokaforge/pull/1983) (open question 3).
- Related code: `core/grading/omitted_fields.py`, `runner/models.py` (`TaskDescription`,
  `SearchConfig`, `JudgeEvidence`, `JudgeCustomization`), `core/models/grade.py`,
  `runner/protocol.py`, `runner/service.py`, `core/shared_stack_runtime.py`,
  `grader/wire_snapshot.py`, `grader/client.py`, `grader/queue.py`,
  `grader/composite_dispatch.py`, `tests/canonical/test_grading_wire_lock.py`.
- External references: #983 (the uncensused trial-spec surface), #1997 (the incident's
  fix).
