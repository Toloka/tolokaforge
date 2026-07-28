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

Before reporting, classify every finding against `{{KNOWN_ISSUES}}` per the shared block's
"Accepted circumstances" rules. The frozen pack is not yours to fix, so:
- a finding already in the registry is an ACCEPTED circumstance: measure it, attribute its pp,
  cite the registry entry, and stop there. No task/golden/simulator edit, no regrade
  recommendation, and do not list it as outstanding work;
- a finding NOT in the registry is a NEW task-pack defect: report it fully and propose the
  registry entry it should become, leaving the accept/footnote/exclude call to the human owner;
- a finding that is really harness/engine/adapter side is NOT a circumstance - report it
  actionably even when its symptom looks like a task defect. Two symptom classes that have
  fooled this dimension before: an expected state that looks authored-wrong but is actually a
  broken golden replay, and a "the task never delivers X" conclusion where the harness
  delivers X and then discards it. Check which side owns the cause before you classify.

RETURN (compact markdown):
- table of oracle/task-design issues (issue x domain x est. trials x est. pp domain+micro x
  ACCEPTED/NEW/HARNESS)
- total estimated FALSE-failure pp on the micro number, split ACCEPTED vs NEW (this pp is the
  ORACLE share that `four-bucket` also reports - it is the same pp, not additional)
- for any unwinnable claim, cite the specific input field you searched in `task.yaml` and
  found absent. Prefer per-task evidence over inference: if the data covers other models,
  a task no model has ever solved is the defensible unwinnable class, while one that someone
  solved is winnable however punishing
- explicitly flag any finding whose burden looks UNEVEN across models - that is rank-distorting
  and stays a finding even if the underlying defect is accepted
- VERDICT: footnote-only (including the accepted-circumstance case), or an oracle fix + regrade
  needed before publishing; name the single highest-impact fix, and say plainly if the highest-
  impact fix is on the harness side rather than in the pack. If every finding is accepted, say
  "footnote-only, all accepted" rather than inventing work.
