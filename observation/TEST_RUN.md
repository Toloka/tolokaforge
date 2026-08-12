# Disposable test branch

Exercises the auto-integration end to end against
`feat/auto-integrate-models-only` (PR #1067): models-wheel-only commits, the
Bucket B refusal, and the derived public-API boundary audit.

Candidate: `azure_ai/cohere-command-a-plus-05-2026` — chosen because its
`<|START_TEXT|>` wrapper needs an `assistant_text_policy`, i.e. a NEW per-model
class, which is exactly the path this PR is meant to move off the engine.

**Never merged.** Delete after the run.
