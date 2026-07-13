# Dimension: preset-codec-leak

Prepend `_shared_context.md`.

QUESTION: did the intended preset apply on every trial, with no reasoning-leak and
no schema-loss? Conclude clean-native, or name the exact policy fix (the resolve
stage `reprobe.py` will prove it).

MODE: in EVAL mode this is a fidelity check - confirm the intended preset applied and no
code/schema/codec bug artificially depressed the score (the number must reflect the model, not
a policy/code error). In OBSERVE mode this is the policy-fix search - name the preset to SET or
CREATE that closes the candidate's FORMATTING failures (reprobe.py proves it), reading the
signal from `findings.json` `failure_messages` + `rejected_examples`, not grade.yaml.

1. Effective preset / routing: confirm it applied on ~100% of trials. The authoritative
   source is `task.yaml` -> `model_config.<role>.resolved.effective_preset` (NOT
   `run_state.json`, which has no preset field); corroborate with `metrics.yaml`
   (reasoning_tokens present) and `prompts.yaml` (prompt_policy injection visible). For
   reasoning models, thinking-signature payloads should decode to `{{MODEL}}` with 0
   foreign-model markers.
2. Reasoning codec: `reasoning_tokens > 0` where expected; reasoning NOT leaking into
   assistant content or tool-call arguments. Grep a broad trajectory sample for literal
   `<thinking>` / `<reasoning>` and for chain-of-thought prose stuffed into tool args or
   the final content.
3. Schema sanitizer: grep a `tools_schemas.yaml` sample for dropped/hollowed params (a
   required name absent from `properties`, an emptied container). Separate schema-loss tool
   errors (a required param the model never received) from legitimate data errors.
4. Recovery loops: any tool+args repeated >=5x in a single trajectory (dict-stringify /
   JSON-encoded-argument recovery loop)? Confirm it is a failure loop, not legit polling.

IMPORTANT (bimodal trap, see the shared block): a ~0% reasoning-leak / schema-loss reading
is AMBIGUOUS. It means "quirk genuinely absent" OR "a shipped preset is already masking it".
If a non-default preset is active (from `task.yaml` effective_preset), "clean-native" is the
WRONG verdict - the correct one is "preset X required" (name it), not "no fix".

RETURN (compact markdown):
- preset-applied %, active preset name, reasoning-present %, reasoning-leak count (expect ~0)
- sanitizer/formatting-failure count attributable to config (expect ~0) + one example
- retry-loop count
- VERDICT: clean-native (default preset AND no leak/loss) OR the exact policy axis + fix
  combination needed (name the preset even if it is already the active non-default one).
