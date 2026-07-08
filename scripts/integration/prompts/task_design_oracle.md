# Dimension: task-design-oracle

Prepend `_shared_context.md`.

Find FALSE failures (the model did the right thing but was graded fail) and
unwinnable/ambiguous tasks. This gates publishability: footnote vs regrade.

MODE: EVAL only. A false-failure means the number is wrong because of the ORACLE, not the model
- a direct fidelity threat. Synthetic observe probes carry their own assert as the oracle, so
skip this dimension on an OBSERVE artifact.

Hunt specifically:
- order-sensitive set/list equality (e.g. a `tags` field graded by exact list order),
  correct SET but wrong ORDER;
- over-creation penalties: a false-failure ONLY if the spec is silent/ambiguous about
  whether extra entities are allowed; if the spec clearly prohibits extras and the model
  over-created, that is GENUINE-MODEL, not an oracle issue (this is the same call
  `four-bucket` makes - use the same spec source so the two agree);
- correct refusal / abstention graded fail (the model rightly declined a disallowed or
  impossible action but the oracle expected the action);
- unwinnable tasks (required info absent from the inputs; no tool to perform a required step);
- cosmetic `state_diff` (ordering / whitespace / equivalent representation) and row-identity
  splits that are not semantic differences.

Method: grep `grade.yaml` `reasons` / `state_diff` for ordering, tag, and list-diff
patterns; find failing trials whose diff is a pure permutation or cosmetic; sample-read
those trajectories to confirm the model's action was actually correct. Read the task inputs
from `task.yaml` (and the system prompt from `prompts.yaml`) - do NOT judge a task unwinnable
from the trajectory alone. For each issue estimate the affected trial count and its pp impact
(per-domain and micro). Be strict: a dropped/added set member is a GENUINE content error, not
an ordering artifact.

RETURN (compact markdown):
- table of oracle/task-design issues (issue x domain x est. trials x est. pp domain+micro)
- total estimated FALSE-failure pp on the micro number (this pp is the ORACLE share that
  `four-bucket` also reports - it is the same pp, not additional)
- for any unwinnable claim, cite the specific input field you searched in `task.yaml` and
  found absent
- VERDICT: footnote-only, or an oracle fix + regrade needed before publishing; name the
  single highest-impact fix.
