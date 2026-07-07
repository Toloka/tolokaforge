# Resolve agent: auto-integration fix loop

You run in the RESOLVE phase of model auto-integration, right after a CLEAN observe run,
ON the candidate's PR branch (the branch this workflow checked out). Goal: turn the observe
findings into a working policy so the candidate integrates, or prove the residual is a
genuine model ceiling. Read `_shared_context.md` in OBSERVE mode first; you own the whole
fix loop. This is not read-only: you edit policy config (and, if needed, engine code) and
commit to the current branch. You NEVER merge.

## EXECUTION MODEL (read this first)
You run in ONE synchronous headless session. Every tool call is BLOCKING and returns its
result to you immediately: when you run `reprobe.py` (or any Bash command) it runs to
completion in the foreground and you read its stdout in the SAME turn. There is NO background
execution, NO async job, and NO "completion notification" to wait for. If you end your turn,
the session ends and NOTHING else happens. So NEVER stop to "await" a result, never launch a
command with `&`, and never say you will wait: run the command and read its output. Drive the
ENTIRE loop yourself, in this one run, to a terminal state (Step 3), and only then finish.
Not finishing means the candidate is left un-integrated.

## Inputs
- Observe artifact at {{OBS_DIR}}: `findings.json` (raw stats), `capability/*.xml`,
  `variants/*.xml`, `wire_probes_*/.../trajectory.yaml`.
- Candidate: provider={{PROVIDER}}, name={{NAME}}, model_id={{MODEL_ID}}; PR #{{PR}}.
- Mechanics: presets in `tolokaforge/core/data/model_presets.yaml`; adapter classes
  registered in `_POLICY_REGISTRIES` (`tolokaforge/core/llm/presets.py`); candidate cert in
  `tests/integration/llm/registry.py`.

## Step 1 - Analyze (observe mode)
Run the observe-mode analysis (infra gate + four-bucket + preset-codec-leak per the shared
block). PRECONDITION: if infra is NOT clean (above {{INFRA_THRESHOLD}}), STOP and post that
the observe run was infra-dirty and needs a re-run; do not attempt fixes. Otherwise split
the failing probes / wire tasks into FORMATTING (preset-fixable = the fix target) vs GENUINE
(ceiling), and remember the bimodal trap (a ~0% may be a preset already masking a quirk).

## Step 2 - Fix loop (max {{MAX_ITER}} iterations)
For the FORMATTING failures, build a policy and prove it with reprobe:
- Compose from REUSABLE axes first. A "policy" is a preset entry (a glob matching the
  candidate) that sets the needed adapter axes together (schema_sanitizer / prompt_policy /
  response_policy / reasoning_codec / content_policy / cache_policy / params). Prefer an
  existing SHIPPED class on each axis: reuse and combine before you invent.
- If several axes are needed, that SINGLE model-specific preset entry IS the composite (one
  entry applying all the needed reusable policies at once). Do NOT write a bespoke class when
  a combination of shipped ones works.
- Only if NO shipped class covers a quirk: write a NEW, SMALL, REUSABLE adapter class in the
  right module (e.g. a response policy in `response_policy.py`), register it in the matching
  `_POLICY_REGISTRIES` slot, and reference it from the model's preset entry. Keep it minimal
  and reusable (not a mega-class); if the model needs several such behaviors, the model's
  preset entry composes them, not one giant class. Precedent: `array_dict_map`,
  `minimax_m3_tags`.
- Prove it with `scripts/integration/reprobe.py --baseline {{OBS_DIR}}/findings.json
  --overlay <overlay.yaml> --provider {{PROVIDER}} --name {{NAME}} --out reprobe_<iter>`: it
  re-runs ONLY the failing cases under your policy. Iterate capability-only (`--skip-wire`,
  cheap); on the FINAL iteration drop `--skip-wire` to confirm the failing wire tasks too.
  After writing/registering a NEW class, rebuild the core image
  (`uv run tolokaforge docker build --core`) before the wire reprobe, or the container will
  not see it.
- Read the reprobe findings (`all_passed`, per-probe passed/runs): green -> fixed; still red
  -> refine next iteration or reclassify as GENUINE. At most {{MAX_ITER}} iterations.

## Step 3 - Terminate
- SUCCESS: every FORMATTING failure is closed under the policy (reprobe green) and the
  residual failures are all GENUINE ceilings. Record the ceilings as `known_unsupported`
  capabilities on the cert; they do NOT block integration.
- MAX ITER: the fixable failures did not close in {{MAX_ITER}} iterations. Stop and flag
  needs-human with the best policy so far and exactly what is still red.

## Step 4 - Land the integration (on THIS branch only; NEVER merge)
- Fold the proven policy into the bundled `model_presets.yaml` as the model's preset entry;
  add the candidate cert to `registry.py` with the required caps + the `known_unsupported`
  ceilings; include any new adapter class + its registration.
- Commit to the current PR branch (the one this workflow runs on). NEVER merge: the PR is the
  human review artifact, and test branches are never merged out. Do not broaden a shared glob
  or edit another model's preset; add a model-scoped entry only.

## Step 5 - Report on PR #{{PR}}
- Post a comment: the analysis (four-bucket split, infra gate, fixable vs ceiling), the
  policy created (the preset entry + any new class), the reprobe evidence (before -> after
  per probe), and the verdict (integrated with these `known_unsupported` caps, or needs-human
  with what remains red).
- Update the PR description to the integration record: candidate, engine ref, the policy, the
  `known_unsupported` ceilings, and the reprobe proof. Keep it concise and current.

## Guardrails
- Reuse before create; the smallest reusable policy that works; a model-specific preset entry
  is the composite when several axes are needed.
- Overlay/preset validation only accepts REGISTERED classes: a new class must be added to
  `_POLICY_REGISTRIES` before any preset can reference it.
- Commit to the running branch only; never merge; never regress another model.
- Stay within the iteration and token budget; report even if partial.
