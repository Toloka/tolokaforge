# Resolve agent (compose step): propose or refine the candidate's policy

You are ONE iteration of the resolve fix-loop. The WORKFLOW drives the loop: it runs the
reprobe and commits; YOU only reason and write files. Do NOT run reprobe, git, or gh - the
workflow does those after you. Read `_shared_context.md` (OBSERVE mode) for the failure model.

This is a normal synchronous Claude Code run with file tools: read what you need, write the
policy files, then stop. There is no background job and nothing to await. Be DECISIVE and
turn-economical: read only the findings, `last_reprobe.json`, and the ONE reference adapter
class closest to your fix - do not tour the whole engine. Reason from the failure MECHANISM,
not from exhaustive code reading. You have a bounded turn budget; a stalled iteration wastes it
and pushes the whole integration to needs-human.

## Inputs
- `{{OBS_DIR}}/findings.json` - the observe baseline (raw stats: failing capability/variant
  probes + wire tool-arg rejections).
- `{{OBS_DIR}}/resolve/last_reprobe.json` - the PREVIOUS iteration's reprobe result (absent on
  iteration 1). On a later iteration, read it to see which fix-targets are STILL red and adjust.
- Candidate: provider=`{{PROVIDER}}`, name=`{{NAME}}`, model_id=`{{MODEL_ID}}`.
  Iteration `{{ITER}}` of `{{MAX_ITER}}`.
- Engine: presets in `tolokaforge/core/data/model_presets.yaml`; adapter classes registered in
  `_POLICY_REGISTRIES` (`tolokaforge/core/llm/presets.py`).

## Your task this iteration
1. Classify the failing probes into FORMATTING (preset-fixable = fix-targets) vs GENUINE
   ceilings (known_unsupported) vs flaky-noise (near-pass like 14/15), per the shared block.
   For a tool-arg rejection, check `tools_schemas.yaml` to tell schema-loss (FORMATTING) from a
   genuine wrong/missing value. On iteration > 1, read `last_reprobe.json` and for each
   fix-target still red decide, by MECHANISM, one of two things - do NOT resubmit the same
   overlay:
   - It is a FORMATTING/leak/schema artifact (reasoning bleeding into content or tool args, a
     sanitizer-droppable field, a wrapper the model emits). Then it STAYS a fix-target, but try
     a materially DIFFERENT axis than last time (e.g. response_policy instead of prompt_policy,
     or a new small adapter class). A leak/format issue is policy-fixable by definition; a weak
     first attempt is not evidence of a ceiling.
   - It is a GENUINE capability limit (the model emits a wrong or absent VALUE it cannot produce
     at all, no wrapper/leak involved). Then RECLASSIFY it: move it to `ceilings`
     (known_unsupported) and DROP it from `fix_targets`. An honest ceiling converges the loop;
     chasing an unfixable target just exhausts it. Never reclassify a leak/format failure as a
     ceiling to force convergence - that ships a falsely-pessimistic cert.

   PASS-RATE DISCIPLINE (the cert MUST follow the observe baseline, not a hunch - a deterministic
   `cert_reconcile` gate enforces this at finalize and fails the integration otherwise):
   - A capability whose observe baseline `pass_rate >= 0.9` (e.g. 14/15) is SUPPORTED: put it in
     `required`. NEVER mark it `known_unsupported` against a passing baseline - that is
     falsely-pessimistic, under-credits the model, and hard-fails the gate. (Real miss: mimo's
     `implicit_prompt_caching` passed 14/15 but was wrongly demoted on one cherry-picked run.)
   - `known_unsupported` is for a capability the baseline shows genuinely FAILING (low pass_rate)
     that is ALSO not policy-fixable (not a formatting/serialization artifact). A `0.8-0.9`
     baseline is BORDERLINE: prefer `required`, or leave a dated, specific failure-mode comment -
     do not silently hard-demote it.
   - TAKE A POSITION ON EVERY PROBED CAPABILITY. If `findings.json` has a per-probe result for a
     capability (pass OR fail), it MUST appear in `required` or `known_unsupported` (via
     `decision.json`). A probed-but-undeclared capability silently auto-skips and hard-fails the
     gate. (Real miss: mimo's `re2_pattern_tolerance` passed 15/15 but was left out of the cert.)
2. Compose the policy as a preset OVERLAY at `{{OBS_DIR}}/resolve/overlay.yaml`: ONE preset
   entry whose `match` globs match THIS model only, composing the needed reusable axes
   (schema_sanitizer / prompt_policy / response_policy / reasoning_codec / content_policy /
   cache_policy / params). Reuse shipped classes; that single model-specific entry IS the
   composite when several axes are needed. Validate it:
   `uv run python -c "from tolokaforge.core.llm.presets import validate_overlay_file as v; v('{{OBS_DIR}}/resolve/overlay.yaml')"`
3. If NO shipped class covers a fix-target, write a NEW small reusable adapter class in the
   right module (e.g. a response policy in `response_policy.py`), register it in the matching
   `_POLICY_REGISTRIES` slot AND export it in `tolokaforge/core/llm/__init__.py`, then reference
   it from the overlay. Keep it minimal and reusable (a composite of shipped classes is a preset
   entry, not a mega-class). Re-run the validate command above so the overlay accepts the name.
4. Write `{{OBS_DIR}}/resolve/decision.json`:
   ```
   {"fix_targets": ["<exact junit probe names from findings.json that the overlay should turn green>"],
    "ceilings": ["<Capability enum names that are genuine known_unsupported>"],
    "required": ["<Capability enum names the model passes or the policy makes pass = cert required>"],
    "data_scope_review": <true|false - see below>,
    "needs_human": <true|false - see below>,
    "needs_human_reason": "<one line naming exactly what data is missing - set iff needs_human>",
    "notes": "<one line: what the policy does + why>"}
   ```
   `fix_targets` MUST be the exact `probe` strings from `findings.json` per_probe (e.g.
   `test_recursive_ref_tool_call[simple-openrouter__xiaomi_mimo-v2.5-pro]`). The workflow
   reprobes these under your overlay; ALL must go green for the candidate to integrate. Do NOT
   list ceilings or flaky-noise as fix_targets.

   Set `data_scope_review: true` when your fix RECOVERS AN ARRAY NESTED INSIDE A FREE-FORM / open
   object (an `additionalProperties: true` parent - e.g. a `tags`-like array one level deep). That
   quirk's correct SCOPE is DATA-BOUND: which fields carry the array is NOT in the schema, only in
   the domain data, so a fix that passes every gate here is still scoped only to what the observe
   surfaced. The workflow will route such a fix to a human for a broader domain-scope check before
   merge (even on full convergence) - a locally-green fix can be too narrow (or over-broad) on data
   the pipeline never saw. A fix that touches ONLY SCHEMA-DECLARED array fields (visible type) is
   `data_scope_review: false`.

   Set `needs_human: true` (with a one-line `needs_human_reason`) when you CANNOT produce a correct
   policy because the necessary evidence is NOT in the observe data - a DATA-BOUND quirk whose
   correct scope/shape depends on real domain data the observe never surfaced (e.g. which fields
   carry a nested array under a free-form `additionalProperties: true` object, knowable only from
   the domain's tool schemas + data, not from the failing probe alone). Do NOT fabricate a fix or a
   false ceiling to force convergence: set the flag, name exactly what data is missing, and stop.
   The workflow escalates to a human IMMEDIATELY - it does not burn the remaining iterations. This
   differs from `data_scope_review` (a fix you DID produce, that a human scope-checks): use
   `needs_human` when you cannot responsibly produce the fix AT ALL from what you can see. When
   `needs_human` is true you may leave `fix_targets` empty and skip writing `overlay.yaml`.

   `required` must be EVIDENCE-BACKED, never optimistic. List a capability as `required` ONLY if
   it passed NATIVELY in `findings.json` OR the reprobe shows it green under your overlay. Do NOT
   promote a capability to `required` on a mechanism that cannot support it: e.g. a summary-only
   (OpenAI-style) `reasoning_codec` carries no signed thinking blocks, so `THINKING_EMITS_BLOCKS`
   and the `*_THINKING_REPLAY` caps stay `known_unsupported` under it; a `passthrough` schema that
   only cleared a weak-assertion probe (no 500, args parse) is NOT evidence the emitted VALUE is
   correct. When the mechanism does not clearly support it or the evidence is a weak probe, prefer
   `known_unsupported` (an honest floor) over a `required` that inflates the leaderboard score -
   the draft-PR human gate can always promote it later. BUT the honest-floor bias applies ONLY to
   genuinely-failing or unproven caps: NEVER demote a capability the observe baseline already
   passes at `>= 0.9` to `known_unsupported` (see PASS-RATE DISCIPLINE above) - a passing synthetic
   result outranks any pessimistic hunch, and the `cert_reconcile` gate rejects it.

Write ONLY: `overlay.yaml`, `decision.json`, and any new engine class + its registration/export.
Do NOT run reprobe, commit, push, or comment. Then stop.
