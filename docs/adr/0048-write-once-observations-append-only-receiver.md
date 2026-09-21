# 0048. Write-once observations on an append-only receiver

- **Status:** Proposed
- **Date:** 2026-09-19
- **Deciders:** @bberkes-toloka (proposer), @CiroGamboa (engine owner, review pending)
- **Supersedes:** none
- **Extends:** [0047 - Live tracing: a TrialObserver seam and an OTLP exporter behind the `otel` extra](0047-live-tracing-trial-observer-otel.md)

## Context and Problem Statement

ADR-0047 writes a trial twice over: a provisional root and live rows while the trial runs, then a
complete pass from the persisted bundle at `trial_persisted` that re-sends every observation under
the same ids. That works on a receiver where a re-sent id is an upsert.

The Langfuse 4.x line changes all three assumptions this rests on. In its default write mode
(`events_only`) a receiver:

- takes observations over **OTLP only** (the legacy ingestion events for observations are refused,
  while scores and media keep their routes);
- stores observations **append-only**: a re-sent id becomes a second row, there is no read-time
  dedup and no API to delete one observation;
- makes a trace **be** its root observation, so a trace list is a list of root observations and a
  trace with no root row is in no list.

Two further measured facts shape the design. The version a receiver reports cannot decide which
family it is, because a 4.x receiver in a transitional write mode still accepts the legacy route
while reporting `4.x`; and a nested metadata value travels as a JSON string over OTLP and is
parsed back into an object on read, on both families.

The problem: keep a running trial observable, keep exactly one record per trial, and keep a v3
receiver on today's behaviour, without the engine's core learning anything receiver-shaped.

## Decision Drivers

- Nothing may be written twice, and no existing observation id may move (the offline sibling
  derives the same ids from the same contract).
- A trial in flight must stay reachable, and a reader must be able to tell what the loop reported
  from what the bundle says.
- A trial that never persists must not vanish from the trace list.
- The verdict of a trace must stay correctable after the fact, although the trace's own metadata
  cannot be.
- One converter for both producers: the live path and the offline uploader must emit identical
  spans, so the converter cannot import the engine.
- A receiver that cannot be asked, or an operator who knows better, must be able to force a family.

## Considered Options

- **(A) One export at trial end.** Simple and correct, but nothing is visible while a trial runs,
  which is the point of live tracing.
- **(B) The live rows are the record, completed at the end.** Impossible on an append-only
  receiver: completing means re-sending, and a re-send duplicates.
- **(C) Declared previews plus a write-once record.** The live rows are written under their own
  ids and marked as previews; the record is written once, from the bundle. Chosen.

Within (C), the previews can be parented to the final root (**case R**, the trace has no root until
the trial ends) or hung under a second root. Case R is chosen: a second root would put two rows for
one trial in every trace list, which is worse than a trial that joins the list when it ends.

## Decision

**1. The family is detected by capability, once per run.** `GET /api/public/v2/observations` answers
on a 4.x receiver in every write mode and 404s on a 3.x one. The probe is read-only and runs at run
start, next to the project check; `options.langfuse.server_api` (`auto` by default, or `v3` / `v4`)
overrides it; a receiver that cannot be asked leaves the run on the v3 family, which keeps ADR-0047's
behaviour unchanged. The family lands in the tracing receipt.

**2. The id contract gains preview kinds.** The previewable kinds get a twin prefixed with `p`
(`proot`, `pgen`, `pugen`, `ptool`, `pjgen`, `pjtool`) derived by the same formula. No existing id
moves, a preview id can never collide with a final one, and a reader can exclude previews by their
ids alone. Every preview row is additionally named `preview: ...` and carries `preview: true` in its
metadata, so a human and a naive reader can tell them apart too.

**3. The live rows become previews under a preview root.** At `trial_started` the observer writes
the preview root, whose parent is the **final** root's id, with the trace name, session, the tags
known then, the native fields and the identity metadata. Every live body then goes out under its
preview kind, under the preview root. The trace has no root row until the trial ends; a reviewer
reaches it by its deterministic trace id or by its session.

**4. The record is written once, from the bundle, root last.** At `trial_persisted`, after the
attachment step, the bundle's projection is converted to OTLP spans by an engine-free converter in
the plugin wheel and written once, with the root as the last span, so a reader never sees a root
whose children have not arrived. The attachment manifest rides in the root's metadata as a JSON
string, which the receiver parses back. The trace's name, session, tags, native fields and identity
metadata ride on **every** span, previews included, because this receiver stores and filters them
per observation. `projection: full` is required on this family, since the root comes from the
bundle: a run that asks for less is refused at run start.

**5. The verdict lives in the scores.** The trace's metadata is frozen at that single write, so a
later grading cannot correct it. Scores keep the ingestion route (they are not append-only) and each
carries the grading's own timestamp. The trace-level mirror of the primary grading is joined by a
categorical `primary_grading` score naming the grading it mirrors; both are marked `scope: primary`
in the score metadata, which is what a dashboard filters on. The frozen `pass` / `score` /
`primary_grading` metadata keys stay as of the first write and are documented as such.

**6. A trace whose root can no longer come gets one at run end.** At `run_finished` the observer
writes a minimal error root for every trial that never persisted, whose bundle pass wrote nothing,
or whose root never reached the receiver: name, session, tags, native fields, identity, start,
`status: error` and the reason, with no manifest and no verdict.

**7. Plugin API 4.** The engine's `PLUGIN_API_VERSION` and the wheel's `__api_version__` move
together; a version-3 pairing is rejected at run start, as before.

**8. The receipt says what happened.** Four counters under `extra` (`langfuse.previews_sent`,
`langfuse.final_observations_sent`, `langfuse.error_roots_sent`, `langfuse.roots_unconfirmed`) and
the family in `details`. The first three count what was queued; what left is the receipt's own
`spans_exported` and `spans_dropped`.

## Consequences

- **Delivery becomes at-most-once on the wire.** The transport's own retries are turned off on
  this family, because a retried batch the receiver already wrote is a duplicate that cannot be
  deleted, while a dropped batch is recoverable: the receipt says so and the offline sibling
  completes the trace. For the same reason a root the exporter posted but could not confirm gets
  **no** error root, only a counter and a warning. The single post is the safety argument itself,
  so it is a run-start requirement rather than a best effort: an OpenTelemetry SDK that cannot be
  asked for it fails the run instead of degrading to the retrying exporter.
- A re-run of the same trial under the same run id no longer corrects anything on this family: the
  observations are already there. Changing what a trace says means a new run id. The offline sibling
  reports how many ids it skipped as already present, and refuses the one command that would have
  to rewrite an existing root.
- A trace list shows one row per finished trial and nothing for a trial in flight. Tooling that
  polls the trace list for progress has to poll the session or the known trace id instead.
- Anything that reads "the current verdict" from the trace metadata is wrong on this family and has
  to read the `scope = primary` scores. This is a reporting change, not a storage change.
- The v3 family keeps ADR-0047's path, so a deployment on either receiver runs the same build. The
  legacy writer can be deleted once no receiver of the v3 family is left; the JSON-string manifest,
  which was the open question, was measured to work on 3.205.1 as well.
- Two producers now share one converter module in the plugin wheel, pinned by a span-attribute
  golden on both sides. The converter may not import the engine, which is enforced by test.

## Links

- Related ADRs: [ADR-0047](0047-live-tracing-trial-observer-otel.md) (the seam, the projection, the
  packaging), [ADR-0030](0030-tolokaforge-models-split.md) (the sibling-wheel pattern)
- Related code: `tolokaforge/observability/ids.py` (the preview kinds),
  `tolokaforge/observability/factory.py` (the plugin API version),
  `tolokaforge_langfuse/src/tolokaforge_langfuse/otlp_spans.py` (the engine-free converter),
  `otel.py` (the preview rows, the single write, the error roots), `gradings.py` (the score
  timestamps and the primary pointer), `docs/OBSERVABILITY.md`
- External references: Langfuse v4 write modes and the OpenTelemetry ingestion attribute
  conventions
