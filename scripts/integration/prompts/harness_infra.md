# Dimension: harness-infra

Prepend `_shared_context.md`.

QUESTION: are ANY failures infra-caused rather than the model? (A dirty infra
result invalidates every pass number, so this gates trust in the whole eval.)

MODE: runs in and gates BOTH modes. EVAL reads `logs.yaml` / `grade.yaml`; OBSERVE starts from
`findings.json` (`wire.infra` + `capability_ran`). Infra contamination voids an eval number and
a candidate observation alike.

If `findings.json` exists (observe artifact), start from its pre-counted infra signals
(`wire.infra{rate_limit, status_error, max_turns, stuck, api_error, api_timeout}` and
`capability_ran`), then confirm against `logs.yaml` / `grade.yaml`. Treat `all_passed:false`
with `capability_ran:false` as INFRA (the suite did not run), never as a model fail.

Scan all trials (logs.yaml final status + grade.yaml) and quantify, total and
per-domain:
1. rate_limit / HTTP 429
2. api_error / timeout / connection / 5xx
3. max_turns reached - INFRA ONLY if the cap was too low or a provider fault consumed
   turns. If the model took wrong/circular actions until the cap it is GENUINE-MODEL; if it
   emitted a byte-identical tool+args loop it is FORMATTING (retry-loop). Test: did the turns
   advance task state? were the repeated calls identical args? Count the model-caused
   max_turns separately and do NOT let them dirty the infra gate.
4. stuck / never-finished (no "Trial execution finished" entry)
5. transport-empty completion (HTTP/transport returned nothing) - INFRA; a finished turn
   with empty content is GENUINE-MODEL, count it separately.
6. engine / tool-executor FAULTS (null-args, internal crash) vs LEGITIMATE tool results
   ("id ... not found", a tool validating a bad model argument) - count the latter
   separately and do NOT call them infra.

Cross-reference: of the FAILING trials (grade `binary_pass:false`), what fraction is
attributable to infra (using the max_turns carve-out above).

Beware false positives: ISO-timestamp fractional seconds and `latency_s` floats grep
as "429"/"502"/"504". Confirm a hit appears in a `message:`/`error:` body, not a number.

RETURN (compact markdown):
- infra-category counts (category x total x per-domain-nonzero)
- model-caused max_turns + legit tool-error counts (context only, NOT infra)
- % of all trials with any infra fault; % of FAILURES attributable to infra
- any domain disproportionately hit
- VERDICT: infra CLEAN or NOT, with the one deciding number. CLEAN if infra-attributable
  failures are below {{INFRA_THRESHOLD}} and no single domain is infra-fatal; above that the
  pass numbers are void -> RE-RUN. If {{INFRA_THRESHOLD}} is unset, report the number and
  defer the clean/not call to the human owner.
