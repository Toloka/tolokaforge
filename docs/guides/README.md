# Guides

How-to guides for common tolokaforge patterns. Each guide is anchored to a
working example under [`examples/`](../../examples/) so you can copy the
pattern rather than reconstruct it.

For reference material — the config schema, the task-YAML fields, the tool
list — see the top-level `docs/*.md` files (linked from the repo
[README](../../README.md)). For deep architecture context — the
`RuntimeBackend` protocol, the compose materialisation lifecycle, the ADRs
— see [`docs/architecture/`](../architecture/).

## Available guides

| Guide | Anchored to | What it covers |
| --- | --- | --- |
| [Isolated trials](isolated_trials.md) | [`examples/native/coding/`](../../examples/native/coding/) (works on any task pack) | Choosing between shared and per-trial runtime backends — what isolation buys you, how to opt in, cost tradeoffs |
| [Multi-container tasks](multi_container_tasks.md) | [`examples/native/multi_service/`](../../examples/native/multi_service/) | Authoring a task that declares its own docker-compose stack (extra services beyond the engine defaults) |

More guides land here as multi-container capabilities and other authoring
patterns expand. Contributions welcome — see
[CONTRIBUTING.md](../../CONTRIBUTING.md).
