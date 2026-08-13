# Disposable test branch

First live exercise of the post-split auto-integration, on top of PR #1110
(`param_value_rules`).

The Cohere `tool_choice: auto` gap is pre-declared as DATA in the models wheel,
because it blocks the observe stage outright — every tool probe fails at the
gateway before the model is ever reached, so the resolve agent would never see
anything to work with. It is a precondition, not the thing under test.

What IS under test: whether the agent handles the `<|START_TEXT|>` markers by
writing an `assistant_text_policy` subclass into
`tolokaforge_models/.../policies/`, rather than into the engine.

**Never merged.** Delete after the run.
