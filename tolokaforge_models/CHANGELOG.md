# Changelog

All notable changes to `tolokaforge-models` are documented in this file.
The wheel follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
its release cadence is orthogonal to the `tolokaforge` engine wheel's own
`vX.Y.Z` tag axis. See
[`docs/RELEASING.md`](https://github.com/Toloka/tolokaforge/blob/main/docs/RELEASING.md#pypi-package--tolokaforge-models-models-vxyz-automated).

## v1.0.0 (unreleased)

Initial release. Publishes the `tolokaforge-models` wheel as a sibling of
the `tolokaforge` engine wheel, carrying the model-data tables and the
per-model policy / certificate surface the engine reaches through the
seam module at
[`tolokaforge.core.model_data`](https://github.com/Toloka/tolokaforge/blob/main/tolokaforge/core/model_data.py).

### Feat

- **data**: bundles `pricing.json`, `model_presets.yaml`, and
  `providers.yaml` under
  [`tolokaforge_models/data/`](https://github.com/Toloka/tolokaforge/tree/main/tolokaforge_models/src/tolokaforge_models/data).
  The engine resolves them via `bundled_pricing_path()`,
  `bundled_presets_path()`, and `bundled_providers_path()` — see
  [`docs/RELEASING.md § Downstream data-resource consumers`](https://github.com/Toloka/tolokaforge/blob/main/docs/RELEASING.md#downstream-data-resource-consumers).
- **certificates**: exports the 39 `ModelCertificate` entries in
  `tolokaforge_models.certificates.ALL_MODELS`. The engine's
  `tolokaforge.testing.certify` seam reads the tuple through
  `tolokaforge.core.model_data.bundled_certificates()`.
- **policies**: ships eight per-model policy subclasses across
  `tolokaforge_models/policies/{gemini,minimax,deepseek,inkling}.py`
  (`GeminiSchema`, `GeminiRecursiveSchema`, `ScalarArrayDictMapResponse`,
  `RefResolvingDictMapHints`, `JsonRecursiveCoerceResponse`,
  `ItemRecursiveUnwrapResponse`, `MinimaxM3TagRecoveryResponse`,
  `OpenAISummaryReplayReasoningCodec`).
- **loader**: declares the `tolokaforge.policies` entry-point group
  (see `[project.entry-points."tolokaforge.policies"]` in
  `pyproject.toml`). The engine's
  `tolokaforge.core.model_data.load_policy_registrations()` merges these
  into `_POLICY_REGISTRIES` at engine import time; duplicate keys or
  unknown slots fail loud.
- **api-version**: `__api_version__ = 1` is the integer version of the
  loader contract between this wheel and the engine. Bumped whenever the
  engine-side loader must change to keep reading this wheel's
  registrations.
- **minimum-engine-version**:
  `minimum_engine_version = ">=0.17,<1.0"`. The engine reads this PEP
  440 specifier at `tolokaforge.core.llm.presets` import via
  `_check_minimum_engine_version()` and refuses to boot on an installed
  engine that does not satisfy it — see
  [`docs/RELEASING.md § Bumping minimum_engine_version on the models wheel`](https://github.com/Toloka/tolokaforge/blob/main/docs/RELEASING.md#bumping-minimum_engine_version-on-the-models-wheel).
