# Plan: ADR-0013 status flip Proposed → Accepted

Issue: #369
Branch: chore/adr-0013-status-flip

## Context

`docs/architecture/adr/0013-runtime-backend-per-trial-rpc-methods.md` still reads
`Status: Proposed`, but the implementation shipped long ago: commit `2d160b1`
(#141) moved the five per-trial RPC methods onto `RuntimeBackend`, and #148
shipped `PerTrialRuntimeBackend`. All five methods (`register_trial`,
`execute_tool`, `grade_trial`, `get_state`, `reset_trial`) plus the pre-existing
`cleanup_trial` are present on the `RuntimeBackend` Protocol (`tolokaforge/core/runtime.py`),
on `SharedStackRuntimeBackend` + its Protocol/InMemory doubles
(`tolokaforge/core/shared_stack_runtime.py`), and on `PerTrialRuntimeBackend`
(`tolokaforge/core/per_trial_runtime.py`). `RUNTIME_BACKENDS.md` documents the
pattern. The ADR's own "Status transition" section names the flip condition —
"Accepted once the implementation ships and one release cycle passes" — and both
halves are now satisfied.

## Goal

The ADR-0013 status header reads `Accepted`, its "Status transition" section
records the realised flip with a date, and the ADR index row matches. No changes
to the ADR body's decision content.

## Non-goals

- No changes to the ADR's Context / Decision / Consequences / Follow-ups prose.
- No code changes — this is documentation-only, no compatibility surface touched.
- No renaming of `DockerRunnerAdapter` (that is a separate follow-up the ADR
  itself lists and is out of scope for #369).

## Design note — deviation from the issue's literal recommendation

The issue suggested an inline dated header (`Status: Accepted (originally Proposed
…, flipped …)`). The repo convention is different: all 15 other Accepted ADRs
carry a bare `- **Status:** Accepted`; none embed a dated note inline. The one
dated-status precedent, ADR-0018, uses a separate `- **Amended:** <date> — …`
header line, and a flip is not an amendment. ADR-0013 already owns a dedicated
"Status transition" section built for exactly this moment. So this plan flips the
header to a bare `Accepted` (matching convention) and records the date in the
existing "Status transition" section (its natural home), rather than inlining it
in the header.

## Stages

### Stage 1: Flip ADR-0013 status to Accepted

- **Contract:** Documentation state only. Three edits, one commit:
  1. `docs/architecture/adr/0013-runtime-backend-per-trial-rpc-methods.md` line 3:
     `- **Status:** Proposed` → `- **Status:** Accepted`.
  2. Same file, the "Status transition" section (lines ~81–84): rewrite the second
     bullet so it reads as realised state, not a pending condition — e.g.
     "**Accepted** on 2026-07-15 — the implementation shipped in #141 (RPC methods
     moved onto `RuntimeBackend`) and #148 (`PerTrialRuntimeBackend`), and a
     release cycle passed with no fresh test breakage traceable to the new Protocol
     surface." Keep the first bullet ("**Proposed** on 2026-07-02 …") as-is — it is
     the accurate historical record of when it was proposed, not a "previously X now
     Y" migration note.
  3. `docs/architecture/adr/README.md` line 45: the index row for `0013` changes
     its `Status` cell from `Proposed` to `Accepted`.
- **Behaviour to lock:** None — no runtime behaviour changes. Verification is a
  read-back of the three edited locations (see Validation). No test tier applies;
  adding a test that greps a doc string would be a mock-of-the-obvious and is
  forbidden per the plan quality bar.
- **Compatibility:** Internal-only documentation. No task contract, task-pack
  format, run-config schema, CLI surface, or published API is touched. No CHANGELOG
  entry required (docs-only status flip).
- **Deliverable:** ADR-0013 header reads `Accepted`; its "Status transition"
  section records the dated flip; the index row matches. Nothing else in the repo
  changes.
- **Validation:**
  - `rg -n '^\- \*\*Status:\*\*' docs/architecture/adr/0013-runtime-backend-per-trial-rpc-methods.md`
    → shows `Accepted`.
  - `rg -n '0013' docs/architecture/adr/README.md` → the index row shows `Accepted`.
  - `rg -n 'Proposed' docs/architecture/adr/0013-runtime-backend-per-trial-rpc-methods.md`
    → the only remaining `Proposed` mention is the historical "**Proposed** on
    2026-07-02" line in the Status-transition section (expected).
  - Reviewer confirms no body-prose changes via `git diff`.
- **Doc updates:** The two files above *are* the doc change; there is no separate
  doc to update.

## Discovered issues

- **Fix in this PR:** The issue body references the ADR by a filename that does not
  exist (`0013-runtime-backend-owns-per-trial-rpc-methods.md`); the real file is
  `0013-runtime-backend-per-trial-rpc-methods.md`. No repo change needed — just
  flagged so the implementer edits the correct file. Not a defect.
- **Filed as issues:** None.

## Risks / open questions

- None. Single-file-plus-index cosmetic flip; the implementation claim is verified
  against the source tree and git history.
