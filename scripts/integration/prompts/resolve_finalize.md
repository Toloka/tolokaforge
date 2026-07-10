# Resolve finalize: land the proven policy as the integration

The fix loop CONVERGED: `{{OBS_DIR}}/resolve/overlay.yaml` is proven (the last reprobe went
green on every fix-target). Land it as the model's integration ON THIS BRANCH. Do NOT commit,
push, or comment - the WORKFLOW does that after you. Write files only, then stop. This is a
normal synchronous Claude Code run; nothing to await.

## Inputs
- `{{OBS_DIR}}/resolve/overlay.yaml` - the proven preset (one model-specific entry).
- `{{OBS_DIR}}/resolve/decision.json` - `fix_targets`, `ceilings` (known_unsupported), `required`.
- `{{OBS_DIR}}/resolve/last_reprobe.json` - the final reprobe evidence (per-probe passed/runs).
- `{{OBS_DIR}}/findings.json` - the observe baseline (before -> after comparison).
- Candidate: provider=`{{PROVIDER}}`, name=`{{NAME}}`, model_id=`{{MODEL_ID}}`, PR #`{{PR}}`.

## Tasks (write/edit files only)
1. Fold the overlay preset into `tolokaforge/core/data/model_presets.yaml`: add ONE new entry
   under `presets:` with the overlay's `match` + axes. Leave every other preset untouched (do
   NOT broaden a shared glob). If the compose step wrote a new adapter class, it is already in
   the engine + `_POLICY_REGISTRIES` + `__init__.py`; leave it.
2. Add the candidate cert to `tests/integration/llm/registry.py`: an `MC(...)` entry in `_ALL`
   with `model_id="{{MODEL_ID}}"`, provider/name, `env_key="OPENROUTER_API_KEY"`,
   `required=frozenset({...})` from decision.json `required`, and
   `known_unsupported=frozenset({...})` from decision.json `ceilings`. Match the surrounding
   style and keep model_ids unique (the canonical registry test enforces this).
3. ENSURE PRICING. Verify the candidate's litellm name (`{{NAME}}`) has an entry under `models`
   in `tolokaforge/core/data/pricing.json`. The pre-observe step normally adds it, but ALWAYS
   check and fill it if missing: fetch OpenRouter pricing
   (`curl -s https://openrouter.ai/api/v1/models`, find the object whose `id == "{{NAME}}"`),
   convert per-token `prompt` / `completion` to USD-per-1M (multiply by 1e6), and add
   `"{{NAME}}": {"input": <in>, "output": <out>}` (plus `"cache_read"` if the API reports a
   non-zero `input_cache_read`) in sorted position. Pricing MUST be present: `COST_USD_POPULATED`
   is a CORE capability that can never be a `known_unsupported` ceiling (the cert_reconcile gate
   rejects that), and the auto-merge price gate refuses to merge an unpriced model.
4. Write `{{OBS_DIR}}/resolve/pr_comment.md` - the integration record: the policy created (the
   preset entry + any new adapter class), a before -> after table per fix-target (baseline
   passed/runs vs final reprobe passed/runs), and the `known_unsupported` ceilings. Concise.
5. Write `{{OBS_DIR}}/resolve/pr_body.md` - a concise PR description: candidate, engine ref, the
   policy, the ceilings, and a line that this was integrated via auto-resolve (and, for a
   disposable test branch, that it must not be merged).

Write only. Do NOT commit, push, comment, or run reprobe. Then stop.
