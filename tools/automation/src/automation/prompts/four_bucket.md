# Dimension: four-bucket

Prepend `_shared_context.md`. Requires the FULL-EVAL layout (scored `grade.yaml`).

Classify EVERY failing trial (grade `binary_pass:false`) into exactly one bucket:
INFRA / HARNESS/TASK-DESIGN/ORACLE / FORMATTING / GENUINE-MODEL (definitions + the
multiple-reason precedence are in the shared block). This answers how much of the pass
gap is recoverable AT ALL.

MODE: in EVAL mode the failures come from scored `grade.yaml`, and the recoverable-vs-genuine
split IS the fidelity adjustment (infra + oracle + formatting pp are non-model and come off the
number). In OBSERVE mode the failures come from `findings.json` (`per_probe` passed < runs + wire
`rejected_examples`); there is NO oracle bucket (the probe's own assert is the spec), and the
emphasis is FORMATTING (preset-fixable, the policy target) vs GENUINE (ceiling).

Method: extract `grade.yaml` `reasons` + the logs final status for all failures via
shell; bucket by pattern; then read a small failing-trajectory sample (dimension cap
<=5 per domain, overriding the shared ~8) to confirm the dominant patterns.

Deciding FORMATTING vs GENUINE-MODEL on a tool-argument rejection ("Error executing tool
X: ..."): open `tools_schemas.yaml` for that call. If the required param is absent or
hollowed in the schema the model actually received (schema-loss), it is FORMATTING
(preset-fixable). If the schema was intact and the model supplied a wrong or missing
value, it is GENUINE-MODEL. Never bucket a tool-arg rejection without checking the schema.

Quantify the split as % of failures AND as pp of the micro pass@1 gap (micro base defined
in the shared block). Break out the dominant genuine-model sub-patterns (wrong-value,
missing-step, over-creation, wrong-entity, policy-miss) with rough frequency and the
field/table they cluster on. Over-creation is a GENUINE-MODEL error ONLY if the task spec
prohibits extra entities; if the spec is silent/ambiguous it is an ORACLE false-failure
(the same call `task-design-oracle` makes) - check the spec in `task.yaml`, do not guess.

OWNERSHIP: this dimension is the SOLE owner of the recoverable-vs-genuine pp split. The
oracle pp and formatting pp reported here are the SAME pp that `task-design-oracle` and
`preset-codec-leak` describe in detail; emit them as separate labeled lines so the
synthesizer maps them 1:1 and never sums across dimensions.

This bucket is HARNESS / TASK-DESIGN / ORACLE, so it mixes three kinds of cause with the same pp
weight but very different consequences: already-ACCEPTED circumstances, NEW pack defects, and
live HARNESS/engine/adapter bugs. Split the line three ways on exactly the labels the shared
block's "Accepted circumstances" defines, and use its test (the entry decides, not mere presence
in the registry) so this dimension and `task-design-oracle` emit the SAME label set and the
synthesizer can map them 1:1. All three count toward the recoverable pp and the true-capability
number; only NEW and HARNESS are work items. Never re-label an accepted circumstance as
GENUINE-MODEL to keep the story tidy (the pp is non-model either way), and never park a live
harness bug under ACCEPTED or NEW: NEW ends in "propose a registry entry", which would
institutionalise a bug that is simply fixable.

RETURN (compact markdown):
- bucket split (bucket x count x % of failures x approx pp of micro), one line per bucket
  labeled infra / oracle / formatting / genuine so pp are attributable, with the oracle line
  further split ACCEPTED / NEW / HARNESS; the owned oracle total is all three summed, and each
  sub-label must be stated even when it is 0.0pp
- dominant genuine-model sub-patterns with rough frequency
- FORMATTING (preset-fixable) count stated explicitly (expect ~0 for a clean-native model;
  remember a ~0 may be a preset masking a dormant quirk - see the shared traps note)
- VERDICT: net recoverable pp (oracle + formatting) vs genuine-model pp, with the numbers;
  state these are the owned split (do not double-count downstream).
