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
    "notes": "<one line: what the policy does + why>"}
   ```
   `fix_targets` MUST be the exact `probe` strings from `findings.json` per_probe (e.g.
   `test_recursive_ref_tool_call[simple-openrouter__xiaomi_mimo-v2.5-pro]`). The workflow
   reprobes these under your overlay; ALL must go green for the candidate to integrate. Do NOT
   list ceilings or flaky-noise as fix_targets.

Write ONLY: `overlay.yaml`, `decision.json`, and any new engine class + its registration/export.
Do NOT run reprobe, commit, push, or comment. Then stop.
