"""Declarative preset loader — YAML → :class:`ModelCapabilities`.

Presets live in
[`tolokaforge/core/data/model_presets.yaml`](../../data/model_presets.yaml)
and are composed from a default block, per-model matches, and a per-provider
overlay. See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md) for the full
resolution order.
"""

from __future__ import annotations

import copy
import difflib
import fnmatch
import inspect
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.llm.assistant_text_policy import (
    AssistantTextPolicy,
    PassthroughAssistantText,
)
from tolokaforge.core.llm.cache_policy import (
    AnthropicEphemeralCache,
    CachePolicy,
    NoCache,
)
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.content_policy import (
    AnthropicContent,
    NovaContent,
    OpenAIContent,
    ToolContentPolicy,
)
from tolokaforge.core.llm.message_assembly_policy import (
    MessageAssemblyPolicy,
    NovaMessageAssembly,
    NullMessageAssembly,
)
from tolokaforge.core.llm.params_policy import GenerationParams, ParamsPolicy
from tolokaforge.core.llm.prompt_policy import (
    DictMapHints,
    NoPromptEnrichment,
    SystemPromptPolicy,
)
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    GeminiReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
    ReasoningCodec,
)
from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    JsonCoerceResponse,
    ResponsePolicy,
    StandardResponse,
    UnwrapInputResponse,
)
from tolokaforge.core.llm.schema_sanitizer import (
    PassthroughSchema,
    StrictSchema,
    ToolSchemaSanitizer,
)
from tolokaforge.core.model_data import bundled_presets_path, load_policy_registrations

__all__ = [
    "build_capabilities",
    "get_overlay_path",
    "get_resolved_presets",
    "resolve_effective_preset",
    "resolve_overlay_path",
    "resolve_policy_names",
    "set_overlay_path",
    "validate_overlay_file",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy-name → class registries
# ---------------------------------------------------------------------------

_SCHEMA_SANITIZERS: dict[str, type[ToolSchemaSanitizer]] = {
    "passthrough": PassthroughSchema,
    "strict": StrictSchema,
}

_PROMPT_POLICIES: dict[str, type[SystemPromptPolicy]] = {
    "none": NoPromptEnrichment,
    "dict_map_hints": DictMapHints,
}

_CONTENT_POLICIES: dict[str, type[ToolContentPolicy]] = {
    "openai": OpenAIContent,
    "anthropic": AnthropicContent,
    "nova": NovaContent,
}

_RESPONSE_POLICIES: dict[str, type[ResponsePolicy]] = {
    "standard": StandardResponse,
    "unwrap_input": UnwrapInputResponse,
    "json_coerce": JsonCoerceResponse,
    "array_dict_map": ArrayDictMapResponse,
}

_REASONING_CODECS: dict[str, type[ReasoningCodec]] = {
    "none": NoReasoningCodec,
    "anthropic": AnthropicReasoningCodec,
    "openai": OpenAIReasoningCodec,
    "gemini": GeminiReasoningCodec,
}

_CACHE_POLICIES: dict[str, type[CachePolicy]] = {
    "none": NoCache,
    "anthropic_ephemeral": AnthropicEphemeralCache,
}

_PARAMS_POLICIES: dict[str, type[ParamsPolicy]] = {
    "generation_params": GenerationParams,
}

_MESSAGE_ASSEMBLY_POLICIES: dict[str, type[MessageAssemblyPolicy]] = {
    "null": NullMessageAssembly,
    "nova": NovaMessageAssembly,
}

_ASSISTANT_TEXT_POLICIES: dict[str, type[AssistantTextPolicy]] = {
    "passthrough": PassthroughAssistantText,
}


#: Mapping from preset *slot name* to the in-engine registry of allowed
#: classes. Single source of truth used both by :func:`_validate_overlay`
#: (host-boundary loud-fail on unknown names) and by the unit test that
#: pins the validator-versus-registry sync invariant. Adding a new
#: registry without listing it here is a test failure, not a runtime hole
#: — see ``tests/unit/llm/test_preset_overlay_validation.py``.
_POLICY_REGISTRIES: dict[str, dict[str, type[Any]]] = {
    "schema_sanitizer": _SCHEMA_SANITIZERS,
    "prompt_policy": _PROMPT_POLICIES,
    "content_policy": _CONTENT_POLICIES,
    "response_policy": _RESPONSE_POLICIES,
    "reasoning_codec": _REASONING_CODECS,
    "cache_policy": _CACHE_POLICIES,
    "params_policy": _PARAMS_POLICIES,
    "message_assembly_policy": _MESSAGE_ASSEMBLY_POLICIES,
    "assistant_text_policy": _ASSISTANT_TEXT_POLICIES,
}


def _merge_out_of_tree_policy_registrations() -> None:
    """Merge ``tolokaforge-models`` policy classes onto ``_POLICY_REGISTRIES``.

    Unknown-slot and duplicate-collision both fail loud with a message naming
    the offending pair (see ADR-0030 § "The one seam"). Runs exactly once at
    module import time.
    """
    for slot_name, registrations in load_policy_registrations().items():
        if slot_name not in _POLICY_REGISTRIES:
            raise RuntimeError(
                f"tolokaforge-models registered policy in unknown slot "
                f"{slot_name!r}; engine knows slots: {sorted(_POLICY_REGISTRIES)}"
            )
        for policy_name, cls in registrations.items():
            existing = _POLICY_REGISTRIES[slot_name].get(policy_name)
            if existing is not None and existing is not cls:
                raise RuntimeError(
                    f"tolokaforge-models registration "
                    f"{slot_name}.{policy_name!r} shadows engine class "
                    f"{existing!r} (would-be new: {cls!r})"
                )
            _POLICY_REGISTRIES[slot_name][policy_name] = cls


_merge_out_of_tree_policy_registrations()


_DEFAULT_PRESET_DATA: dict[str, Any] = {"default": {}, "presets": {}, "providers": {}}


# ---------------------------------------------------------------------------
# Overlay registry — operator-overridable preset data
# ---------------------------------------------------------------------------
#
# The bundled ``model_presets.yaml`` is shipped inside the wheel and ties model
# registrations to the engine release cadence. An operator-supplied overlay
# file (CLI ``--presets-file`` / ``RunConfig.engine.presets_file``) lifts that
# constraint: the engine merges the overlay onto the bundled data at startup.
# See [ADR 0002](../../../docs/adr/0002-external-model-registry.md).
#
# Validation runs at load time and is loud (``ValueError``) on any policy-name
# string that does not resolve in the in-engine registries — mirrors the
# ``ensure_registered_adapter`` host-boundary pattern in
# ``tolokaforge.adapters``.

#: Top-level keys allowed in an overlay (and the bundled file).
_OVERLAY_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"default", "presets", "providers"})


def _extract_known_keys(cls: type[Any]) -> frozenset[str]:
    """Return the construction kwargs a policy class accepts.

    Prefers a class-body ``KNOWN_KEYS`` declaration (the authoritative source
    of truth for :class:`ParamsPolicy` subclasses per
    :mod:`tolokaforge.core.llm.params_policy`); falls back to
    :func:`inspect.signature` on ``cls.__init__`` for policy classes that do
    not declare it. Inherited ``KNOWN_KEYS`` are ignored — only class-body
    declarations count, so a subclass that widens the accepted set must say
    so explicitly.

    Variadic ``*args`` / ``**kwargs`` parameters are excluded from the result:
    a policy class without an explicit ``__init__`` inherits
    ``object.__init__``, whose signature is ``(self, /, *args, **kwargs)``.
    Including those literal parameter names would let ``params: {kwargs: [1]}``
    pass overlay validation and fail later at ``cls(**kwargs)``.
    """
    declared = cls.__dict__.get("KNOWN_KEYS")
    if declared is not None:
        return frozenset(declared)
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return frozenset()
    return frozenset(
        name
        for name, param in sig.parameters.items()
        if name != "self"
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    )


def _models_declared_extra_params_keys() -> frozenset[str]:
    """Extra ``params:`` keys declared by an installed models wheel.

    Empty today; the union with :func:`_params_slot_known_keys` keeps overlay
    validation strict on typos while staying silent on legitimate models-wheel
    version skew.
    """
    # TODO(#645): populate from tolokaforge.core.model_data.
    return frozenset()


def _params_slot_known_keys() -> frozenset[str]:
    """Union of every registered :class:`ParamsPolicy` subclass's ``KNOWN_KEYS``.

    The overlay validator consults this to decide which preset ``params:``
    keys are legal at the top-level shorthand (a bare ``params: {...}``
    block, which today's presets all use). Adding a knob is a one-line
    declarative change on the subclass — no engine-wide introspection point
    to maintain. The union widens with ``_models_declared_extra_params_keys``
    to keep overlays silent on legitimate models-wheel skew.
    """
    keys: set[str] = set()
    for cls in _PARAMS_POLICIES.values():
        keys.update(_extract_known_keys(cls))
    keys.update(_models_declared_extra_params_keys())
    return frozenset(keys)


def _normalize_slot_value(
    value: Any,
    *,
    slot: str,
    where: str,
) -> tuple[str, dict[str, Any]]:
    """Coerce a preset slot value into ``(name, params)`` for uniform dispatch.

    Detection:

    * ``type(value) is str`` — legacy bare-name shape; ``params`` empty.
    * ``type(value) is dict`` with a ``name`` key — new shape; ``params`` is
      the nested mapping (defaulting to ``{}``).
    * ``dict`` without ``name`` — raises :class:`ValueError` naming the
      preset, block, and slot; silent no-op would produce hard-to-debug
      drift between the fingerprint and the actual dispatch.
    * Anything else — raises :class:`ValueError` with the observed type.
    """
    if type(value) is str:
        return value, {}
    if type(value) is dict:
        if "name" not in value:
            raise ValueError(
                f"Preset {where} slot {slot!r}: dict shape is missing the "
                f"required 'name' key; got keys={sorted(value.keys())}. "
                f"Expected either a bare name string or "
                f"{{name: str, params: dict}}."
            )
        raw_params = value.get("params") or {}
        if not isinstance(raw_params, dict):
            raise ValueError(
                f"Preset {where} slot {slot!r}.params: expected a mapping, "
                f"got {type(raw_params).__name__}."
            )
        return value["name"], dict(raw_params)
    raise ValueError(
        f"Preset {where} slot {slot!r}: expected a bare name string or "
        f"{{name, params}} mapping, got {type(value).__name__}."
    )


#: Module-level overlay path. ``None`` → bundled-only (today's behaviour).
#: Mutated only via :func:`set_overlay_path` so cache invalidation has one
#: choke point. The autouse ``overlay_isolation`` fixture in
#: ``tests/conftest.py`` calls ``set_overlay_path(None)`` after every test so
#: module state cannot leak between cases.
_OVERLAY_PATH: str | None = None

#: Memoised merged preset data. Cleared by :func:`set_overlay_path`.
_CACHED_PRESETS: dict[str, Any] | None = None


def set_overlay_path(path: str | None) -> None:
    """Set (or clear) the active preset overlay file path.

    Called once at engine startup by the CLI / orchestrator after resolving
    precedence (CLI flag > ``RunConfig.engine.presets_file``). Idempotent:
    calling with ``None`` clears the overlay; calling with the same path
    twice is a no-op-with-cache-clear.

    The first subsequent call to :func:`_load_presets` re-reads the bundled
    YAML and merges in the overlay (if any). Validation errors raise
    ``ValueError`` from :func:`_load_presets`, not from this function, so
    that callers can defer the file-read until the engine actually needs it.
    """
    global _OVERLAY_PATH, _CACHED_PRESETS
    _OVERLAY_PATH = path
    _CACHED_PRESETS = None


def get_overlay_path() -> str | None:
    """Return the active overlay path, or ``None`` if no overlay is installed.

    Read-only accessor — :func:`set_overlay_path` is the sole mutator.
    Useful for orchestrator code that wants to persist the active overlay
    into queue run-state so worker subprocesses can inherit it.
    """
    return _OVERLAY_PATH


def resolve_overlay_path(
    cli_value: str | None = None,
    config_value: str | None = None,
) -> str | None:
    """Resolve the overlay path with precedence ``cli > config``.

    Returns the first non-empty value, or ``None`` if both are unset.
    Exists as a shared helper so the CLI, the config validator, and the
    orchestrator all agree on precedence semantics.
    """
    if cli_value:
        return cli_value
    if config_value:
        return config_value
    return None


def _load_bundled_presets() -> dict[str, Any]:
    """Load the bundled ``model_presets.yaml`` via the model-data seam.

    Raises
    ------
    FileNotFoundError
        The bundled preset file is absent — propagated from the accessor.
    ValueError
        The YAML parsed to ``None`` (empty file) or a non-mapping payload.
        A corrupted install shape must surface as a loud startup failure.
    """
    preset_path = bundled_presets_path()
    with open(preset_path) as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ValueError(f"bundled preset table {preset_path} is empty")
    if not isinstance(data, dict):
        raise ValueError(
            f"bundled preset table {preset_path} must be a mapping, got {type(data).__name__}"
        )
    return data


def _load_overlay_file(path: str) -> dict[str, Any]:
    """Read + parse + validate an operator-supplied overlay file.

    Loud-fail on every recognised mis-configuration: missing file, malformed
    YAML, non-mapping top-level, unknown top-level keys, unknown policy-name
    strings, unknown ``params:`` keys. The error message always names the
    overlay path so the operator can find the offending file.
    """
    overlay_path = Path(path)
    if not overlay_path.exists():
        raise FileNotFoundError(
            f"Preset overlay file not found: {path!r}. "
            f"Check the value of --presets-file or engine.presets_file in the "
            f"run config."
        )
    try:
        with open(overlay_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Preset overlay {path!r} failed to parse as YAML: {exc}") from exc

    if data is None:
        # Empty file is allowed and is equivalent to the default-empty registry.
        return dict(_DEFAULT_PRESET_DATA)
    if not isinstance(data, dict):
        raise ValueError(
            f"Preset overlay {path!r} must be a YAML mapping at the top level, "
            f"got {type(data).__name__}."
        )

    _validate_overlay(data, path)
    return data


def validate_overlay_file(path: str) -> None:
    """Public host-boundary check for an overlay file.

    Raises :class:`FileNotFoundError` if the file is absent, or
    :class:`ValueError` on any recognised schema / policy-name
    misconfiguration. Errors always name the file path so the operator
    can find the offending file. Used by ``tolokaforge config validate``
    and by the ``run`` / ``prepare`` / ``worker`` CLIs to fail eagerly
    before any orchestrator / Docker startup work is done.
    """
    _load_overlay_file(path)


def _validate_overlay(data: dict[str, Any], path: str) -> None:
    """Host-boundary validation for an overlay's contents.

    Every policy-name string in the overlay must resolve in the in-engine
    registries (``_SCHEMA_SANITIZERS`` … ``_CACHE_POLICIES``); unknown names
    raise ``ValueError`` with the file path and the offending key. Mirrors
    ``ensure_registered_adapter()`` in ``tolokaforge.adapters``.

    A new policy class added to the engine must also be added to the
    appropriate registry (it would not be reachable from YAML otherwise);
    overlay validation then accepts it automatically. The
    ``test_preset_overlay_validator_registry_sync`` unit test pins this
    invariant.
    """
    unknown_top = set(data.keys()) - _OVERLAY_TOP_LEVEL_KEYS
    if unknown_top:
        raise ValueError(
            f"Preset overlay {path!r} has unknown top-level keys: "
            f"{sorted(unknown_top)}. Allowed: {sorted(_OVERLAY_TOP_LEVEL_KEYS)}."
        )

    def _check_block(block: dict[str, Any], where: str) -> None:
        if not isinstance(block, dict):
            raise ValueError(
                f"Preset overlay {path!r} at {where}: expected a mapping, "
                f"got {type(block).__name__}."
            )
        # Mutex: legacy top-level ``params:`` shorthand and the new
        # slot-nested ``params_policy:`` shape must not coexist. Silently
        # merging or dropping either source would produce hard-to-debug
        # drift between the resolved fingerprint and the actual constructor
        # kwargs handed to ``ParamsPolicy.__init__``.
        if "params_policy" in block and "params" in block:
            raise ValueError(
                f"Preset overlay {path!r} at {where}: conflicting "
                f"'params' and 'params_policy' keys. The top-level 'params' "
                f"shorthand and the slot-nested 'params_policy' shape are "
                f"mutually exclusive — pick one so the constructor kwargs "
                f"resolved for GenerationParams (or its subclass) are "
                f"unambiguous."
            )
        for slot, registry in _POLICY_REGISTRIES.items():
            if slot not in block:
                continue
            slot_where = f"{where}.{slot}"
            name, nested_params = _normalize_slot_value(block[slot], slot=slot, where=slot_where)
            if name not in registry:
                raise ValueError(
                    f"Preset overlay {path!r} at {where}: unknown "
                    f"{slot} {name!r}. Available: {sorted(registry.keys())}. "
                    f"New policy classes require an engine release; "
                    f"overlays can only reference existing ones."
                )
            if nested_params:
                allowed = _extract_known_keys(registry[name])
                if slot == "params_policy":
                    allowed = allowed | _models_declared_extra_params_keys()
                _reject_unknown_params(nested_params, allowed, f"{slot_where}.params")
        params = block.get("params")
        if params:
            if not isinstance(params, dict):
                raise ValueError(
                    f"Preset overlay {path!r} at {where}.params: "
                    f"expected a mapping, got {type(params).__name__}."
                )
            _reject_unknown_params(params, _params_slot_known_keys(), f"{where}.params")

    def _reject_unknown_params(
        params: dict[str, Any],
        allowed: frozenset[str],
        where: str,
    ) -> None:
        """Fail loud on unknown ``params:`` keys with closest-match hints."""
        unknown = set(params) - allowed
        if not unknown:
            return
        hints: list[str] = []
        for key in sorted(unknown):
            close = difflib.get_close_matches(key, allowed, n=3, cutoff=0.6)
            if close:
                hints.append(f"{key!r} (did you mean: {close})")
            else:
                hints.append(repr(key))
        raise ValueError(
            f"Preset overlay {path!r} at {where}: unknown keys "
            f"{sorted(unknown)} — {'; '.join(hints)}. "
            f"Allowed: {sorted(allowed)}."
        )

    if "default" in data:
        _check_block(data["default"] or {}, "default")

    for preset_name, preset in (data.get("presets") or {}).items():
        _check_block(preset or {}, f"presets.{preset_name}")

    for provider_name, provider_cfg in (data.get("providers") or {}).items():
        _check_block(provider_cfg or {}, f"providers.{provider_name}")


def _merge_overlay(
    bundled: dict[str, Any], overlay: dict[str, Any], overlay_path: str
) -> dict[str, Any]:
    """Merge ``overlay`` onto ``bundled``.

    - ``default:`` shallow merge; overlay wins; nested ``params`` merges
      deeply.
    - ``presets:`` overlay entries are **prepended** to iteration order so
      first-match-wins lets operators shadow a bundled preset. Same-named
      overlay entries replace the bundled entry; the replacement is logged
      at INFO so the operator can confirm it took effect.
    - ``providers:`` shallow merge per provider key; overlay wins; nested
      ``params`` merges deeply.
    """
    merged: dict[str, Any] = {}

    # default block
    default_bundled = bundled.get("default") or {}
    default_overlay = overlay.get("default") or {}
    merged_default = dict(default_bundled)
    for key, value in default_overlay.items():
        if key == "params" and isinstance(merged_default.get("params"), dict):
            merged_default["params"] = {**merged_default["params"], **value}
        else:
            merged_default[key] = value
    merged["default"] = merged_default

    # presets block — overlay first (iteration order matters for first-match-wins),
    # then bundled minus shadowed names.
    bundled_presets = bundled.get("presets") or {}
    overlay_presets = overlay.get("presets") or {}
    new_presets: dict[str, Any] = {}
    for name, preset in overlay_presets.items():
        if name in bundled_presets:
            logger.info(
                "preset overlay %r shadows bundled preset %r",
                overlay_path,
                name,
            )
        new_presets[name] = preset
    for name, preset in bundled_presets.items():
        if name in overlay_presets:
            continue  # already inserted from overlay
        new_presets[name] = preset
    merged["presets"] = new_presets

    # providers block
    bundled_providers = bundled.get("providers") or {}
    overlay_providers = overlay.get("providers") or {}
    new_providers: dict[str, Any] = {}
    for name in list(bundled_providers) + [
        n for n in overlay_providers if n not in bundled_providers
    ]:
        b = bundled_providers.get(name) or {}
        o = overlay_providers.get(name) or {}
        merged_p = dict(b)
        for key, value in o.items():
            if key == "params" and isinstance(merged_p.get("params"), dict):
                merged_p["params"] = {**merged_p["params"], **value}
            else:
                merged_p[key] = value
        new_providers[name] = merged_p
    merged["providers"] = new_providers

    return merged


def _load_presets() -> dict[str, Any]:
    """Load model capability presets — bundled YAML merged with overlay if set.

    Module-level cached. Invalidate via :func:`set_overlay_path`. The cache
    is process-local; subprocess workers re-run this fresh on first call.
    """
    global _CACHED_PRESETS
    if _CACHED_PRESETS is not None:
        return _CACHED_PRESETS

    bundled = _load_bundled_presets()
    if _OVERLAY_PATH is None:
        _CACHED_PRESETS = bundled
        return _CACHED_PRESETS

    overlay = _load_overlay_file(_OVERLAY_PATH)
    _CACHED_PRESETS = _merge_overlay(bundled, overlay, _OVERLAY_PATH)
    return _CACHED_PRESETS


def get_resolved_presets() -> dict[str, Any]:
    """Return the resolved (bundled ⊕ overlay) preset table as a defensive copy.

    Public accessor for callers that need the merged preset state as data
    — the model-data fingerprint (see
    :func:`tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`),
    diagnostics tools, and any other caller that needs the merged data
    snapshot without re-reading YAML. Returns a fresh ``dict`` on every
    call so callers cannot mutate the module cache.
    """
    return copy.deepcopy(_load_presets())


def _iter_preset_matches(
    model_name: str,
    provider: str,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(preset_name, preset_dict)`` pairs whose match globs match.

    Iterates over every preset whose ``match`` / ``match_provider`` globs match
    ``(model_name, provider)``. Iteration order follows YAML declaration order
    — callers that want first-match-wins semantics should ``break`` / ``return``
    after the first yield.

    Shared by :func:`_match_preset` (merged-config lookup) and
    :func:`resolve_effective_preset` (name-only lookup) so that the two paths
    cannot drift.
    """
    data = _load_presets()
    name_lower = model_name.lower()
    provider_lower = (provider or "").lower()
    for preset_name, preset in data.get("presets", {}).items():
        patterns = preset.get("match", [])
        provider_patterns = preset.get("match_provider", [])
        matched = any(fnmatch.fnmatch(name_lower, p) for p in patterns)
        if not matched and provider_patterns:
            matched = any(fnmatch.fnmatch(provider_lower, p) for p in provider_patterns)
        if matched:
            yield preset_name, preset


def _match_preset(model_name: str, provider: str) -> dict[str, Any]:
    """Find the first matching model preset.

    Returns merged config: default → matched preset → provider override.
    """
    data = _load_presets()
    result: dict[str, Any] = dict(data.get("default", {}))

    # Match model preset — first match wins
    for _preset_name, preset in _iter_preset_matches(model_name, provider):
        for key, value in preset.items():
            if key in ("match", "match_provider"):
                continue
            if key == "params" and "params" in result:
                result["params"] = {**result["params"], **value}
            else:
                result[key] = value
        break

    # Apply provider overlay
    prov_lower = provider.lower().split("/")[0]  # "openrouter/google" → "openrouter"
    provider_cfg = data.get("providers", {}).get(prov_lower, {})
    for key, value in provider_cfg.items():
        if key == "params" and "params" in result:
            result["params"] = {**result["params"], **value}
        else:
            result[key] = value

    return result


#: Every override key recognised by :func:`_apply_config_overrides`.
#: Keep synchronised with the body below — the unit-test
#: ``test_recognised_keys_are_the_documented_set`` asserts every key in this
#: set appears as a literal inside the function body. Adding a new override
#: key means (a) updating this set, (b) extending the conditional logic.
_RECOGNISED_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        # Prompt policy
        "dict_map_prompt_hints",
        # Schema sanitizer
        "supports_schema_extras",
        "supports_typed_dict_maps",
        # Response policy
        "unwrap_input_key",
        # Content policy
        "tool_content_format",
        "supports_tool_images",
        # Reasoning codec
        "gemini_drop_placeholder_signature",
        # Params policy
        "fixed_temperature",
        "supports_seed",
        "reasoning_via_extra_body",
        "reasoning_via_thinking_kwarg",
        "drop_sampling_when_thinking",
        "reasoning_budget_default",
    }
)


def _apply_config_overrides(cfg: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Translate run-config override keys into preset-format fields.

    Mutates *cfg* in place.

    Raises
    ------
    ValueError
        If any key in *overrides* is not listed in
        :data:`_RECOGNISED_OVERRIDE_KEYS`. Typos in run-config YAML must
        surface loudly rather than silently no-op, per AGENTS.md rule #1
        ("Surface failures explicitly"). See ``docs/CONFIG.md`` §
        ``ModelConfig.capabilities`` for the current contract.
    """
    unknown = set(overrides) - _RECOGNISED_OVERRIDE_KEYS
    if unknown:
        raise ValueError(
            f"Unknown capability override keys: {sorted(unknown)}. "
            f"Recognised keys: {sorted(_RECOGNISED_OVERRIDE_KEYS)}. "
            f"See docs/CONFIG.md § ModelConfig.capabilities for the contract."
        )

    # dict_map_prompt_hints → prompt_policy
    if overrides.get("dict_map_prompt_hints"):
        cfg["prompt_policy"] = "dict_map_hints"

    # Schema strictness overrides
    if (
        overrides.get("supports_schema_extras") is False
        or overrides.get("supports_typed_dict_maps") is False
    ):
        if cfg.get("schema_sanitizer", "passthrough") == "passthrough":
            cfg["schema_sanitizer"] = "strict"

    # Response wrapping
    if overrides.get("unwrap_input_key"):
        cfg["response_policy"] = "unwrap_input"

    # Content format overrides
    fmt = overrides.get("tool_content_format")
    if fmt in ("anthropic", "openai"):
        cfg["content_policy"] = fmt

    if "supports_tool_images" in overrides:
        current = cfg.get("content_policy", "openai")
        if overrides["supports_tool_images"] and current != "anthropic":
            cfg["content_policy"] = "anthropic"
        elif not overrides["supports_tool_images"] and current != "openai":
            cfg["content_policy"] = "openai"

    # Reasoning-codec constructor kwargs flow through the ordinary
    # ``{name, params}`` slot shape; this reroutes the legacy top-level
    # ``gemini_drop_placeholder_signature`` capability override into the
    # slot's ``params`` block so dispatch stays uniform in
    # ``build_capabilities`` — no model-name conditional required.
    if "gemini_drop_placeholder_signature" in overrides:
        existing = cfg.get("reasoning_codec", "none")
        name, params = _normalize_slot_value(
            existing, slot="reasoning_codec", where="<capabilities-override>"
        )
        cfg["reasoning_codec"] = {
            "name": name,
            "params": {
                **params,
                "drop_placeholder_signature": bool(overrides["gemini_drop_placeholder_signature"]),
            },
        }

    # Params-level overrides — every key below lands on ``GenerationParams``
    # (see tolokaforge/core/llm/params_policy.py).
    params = cfg.setdefault("params", {})
    if "fixed_temperature" in overrides:
        params["fixed_temperature"] = overrides["fixed_temperature"]
    if "supports_seed" in overrides:
        params["supports_seed"] = overrides["supports_seed"]
    if "reasoning_via_extra_body" in overrides:
        params["reasoning_via_extra_body"] = overrides["reasoning_via_extra_body"]
    if "reasoning_via_thinking_kwarg" in overrides:
        params["reasoning_via_thinking_kwarg"] = overrides["reasoning_via_thinking_kwarg"]
    if "drop_sampling_when_thinking" in overrides:
        params["drop_sampling_when_thinking"] = overrides["drop_sampling_when_thinking"]
    if "reasoning_budget_default" in overrides:
        params["reasoning_budget_default"] = overrides["reasoning_budget_default"]


#: Per-slot default policy name used when a preset block omits the slot.
_SLOT_DEFAULTS: dict[str, str] = {
    "schema_sanitizer": "passthrough",
    "prompt_policy": "none",
    "content_policy": "openai",
    "response_policy": "standard",
    "reasoning_codec": "none",
    "cache_policy": "none",
    "params_policy": "generation_params",
    "message_assembly_policy": "null",
    "assistant_text_policy": "passthrough",
}


def _instantiate_slot(
    cfg: dict[str, Any],
    slot: str,
    where: str,
    *,
    extra_params: dict[str, Any] | None = None,
) -> Any:
    """Resolve ``cfg[slot]`` to a policy instance via the uniform ``{name, params}`` shape.

    ``extra_params`` merges into the resolved constructor kwargs (rightmost
    wins) so callers can layer additional kwargs on top of the preset —
    used by ``params_policy`` to route the legacy top-level ``params:``
    shorthand through the same dispatch as ``{name, params}``. The MUTEX
    at overlay-load time guarantees the two sources never overlap.
    """
    value = cfg.get(slot, _SLOT_DEFAULTS[slot])
    name, params = _normalize_slot_value(value, slot=slot, where=where)
    registry = _POLICY_REGISTRIES[slot]
    if name not in registry:
        raise ValueError(
            f"Preset for {where} slot {slot!r}: unknown policy {name!r}. "
            f"Available: {sorted(registry.keys())}."
        )
    kwargs = {**params, **(extra_params or {})}
    return registry[name](**kwargs)


def build_capabilities(
    model_name: str,
    provider: str = "",
    overrides: dict[str, Any] | None = None,
) -> ModelCapabilities:
    """Build :class:`ModelCapabilities` from presets + config overrides.

    Resolution order: default → matched preset → provider overlay → overrides.
    """
    cfg = _match_preset(model_name, provider)

    if overrides:
        _apply_config_overrides(cfg, overrides)

    where = f"model={model_name!r} provider={provider!r}"

    # MUTEX re-check on the effective merged config. ``_check_block`` fires
    # only when both keys appear in the same overlay-file block; a bundled
    # ``params: {...}`` and an overlay ``params_policy: {name, params}`` can
    # still land side-by-side after per-key merge. Route ambiguity surfaces
    # loud here rather than silently favouring one source.
    if "params_policy" in cfg and cfg.get("params"):
        raise ValueError(
            f"Effective preset for {where} carries both 'params' and "
            f"'params_policy'. The top-level 'params' shorthand and the "
            f"slot-nested 'params_policy' shape are mutually exclusive — one "
            f"landed via the bundled preset and the other via an overlay. "
            f"Remove one so the constructor kwargs resolved for "
            f"GenerationParams (or its subclass) are unambiguous."
        )

    # ``params_policy`` accepts a slot-nested ``{name, params}`` OR a
    # top-level ``params:`` shorthand (every shipped preset uses the
    # shorthand). MUTEX-enforced at overlay-load; here we route the
    # shorthand into ``extra_params`` so both paths land through the same
    # registry dispatch.
    legacy_params = cfg.get("params") or {}
    params_extra: dict[str, Any] | None = dict(legacy_params) if legacy_params else None

    schema = _instantiate_slot(cfg, "schema_sanitizer", where)
    prompt = _instantiate_slot(cfg, "prompt_policy", where)
    content = _instantiate_slot(cfg, "content_policy", where)
    response = _instantiate_slot(cfg, "response_policy", where)
    reasoning = _instantiate_slot(cfg, "reasoning_codec", where)
    cache = _instantiate_slot(cfg, "cache_policy", where)
    params = _instantiate_slot(cfg, "params_policy", where, extra_params=params_extra)
    message_assembly = _instantiate_slot(cfg, "message_assembly_policy", where)
    assistant_text = _instantiate_slot(cfg, "assistant_text_policy", where)

    api_call_timeout_s = cfg.get("api_call_timeout_s")
    api_call_retries = cfg.get("api_call_retries")
    api_call_wall_timeout_s = cfg.get("api_call_wall_timeout_s")

    return ModelCapabilities(
        schema_sanitizer=schema,
        prompt_policy=prompt,
        content_policy=content,
        params_policy=params,
        response_policy=response,
        reasoning_codec=reasoning,
        cache_policy=cache,
        message_assembly_policy=message_assembly,
        assistant_text_policy=assistant_text,
        api_call_timeout_s=float(api_call_timeout_s) if api_call_timeout_s is not None else None,
        api_call_retries=int(api_call_retries) if api_call_retries is not None else None,
        api_call_wall_timeout_s=(
            float(api_call_wall_timeout_s) if api_call_wall_timeout_s is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Preset fingerprint helpers
# ---------------------------------------------------------------------------
#
# Downstream consumers (analytics tools, the per-trial ``task.yaml`` writer)
# need a stable, JSON-serialisable view of WHICH preset + WHICH policies
# landed on a given :class:`ModelCapabilities`. These helpers perform the
# reverse lookup: "given a capabilities instance, what preset names does the
# registry reveal?"
#
# ``params_policy`` is intentionally omitted from the fingerprint — it is a
# stateful :class:`GenerationParams` dataclass, not a single-named policy, and
# round-trips independently via ``ModelConfig`` serialisation.


def _reverse_lookup(
    instance: Any,
    registry: dict[str, type[Any]],
    slot_name: str,
) -> str:
    """Find the registry name whose class matches ``type(instance)``.

    Raises ``ValueError`` if no match — surfaces configuration drift rather
    than silently emitting a stub (AGENTS.md rule #1).
    """
    instance_cls = type(instance)
    for name, cls in registry.items():
        if cls is instance_cls:
            return name
    raise ValueError(
        f"Unknown policy instance for slot {slot_name!r}: "
        f"{instance_cls.__name__} not in registry "
        f"{sorted(registry.keys())!r}"
    )


def resolve_policy_names(capabilities: ModelCapabilities) -> dict[str, str]:
    """Reverse-lookup from policy instances on *capabilities* to registry names.

    Returns a dict shaped for ``task.yaml.model_config.<role>.resolved.*``
    consumption::

        {
            "schema_sanitizer":        "strict" | "passthrough",
            "prompt_policy":           "dict_map_hints" | "none",
            "content_policy":          "anthropic" | "openai" | "nova",
            "response_policy":         "standard" | "array_dict_map" | "unwrap_input",
            "reasoning_codec":         "anthropic" | "openai" | "none",
            "cache_policy":            "anthropic_ephemeral" | "none",
            "message_assembly_policy": "null" | "nova",
            "assistant_text_policy":   "passthrough",
        }

    ``params_policy`` is omitted — it is a stateful dataclass, not a single
    named policy. Callers that need the full parameter block should serialise
    ``ModelConfig`` alongside this fingerprint.

    Raises
    ------
    ValueError
        If any slot holds a policy instance not registered in the
        corresponding name → class map. This is an explicit failure signal
        rather than a silent pass-through, per AGENTS.md rule #1.
    """
    return {
        "schema_sanitizer": _reverse_lookup(
            capabilities.schema_sanitizer, _SCHEMA_SANITIZERS, "schema_sanitizer"
        ),
        "prompt_policy": _reverse_lookup(
            capabilities.prompt_policy, _PROMPT_POLICIES, "prompt_policy"
        ),
        "content_policy": _reverse_lookup(
            capabilities.content_policy, _CONTENT_POLICIES, "content_policy"
        ),
        "response_policy": _reverse_lookup(
            capabilities.response_policy, _RESPONSE_POLICIES, "response_policy"
        ),
        "reasoning_codec": _reverse_lookup(
            capabilities.reasoning_codec, _REASONING_CODECS, "reasoning_codec"
        ),
        "cache_policy": _reverse_lookup(capabilities.cache_policy, _CACHE_POLICIES, "cache_policy"),
        "message_assembly_policy": _reverse_lookup(
            capabilities.message_assembly_policy,
            _MESSAGE_ASSEMBLY_POLICIES,
            "message_assembly_policy",
        ),
        "assistant_text_policy": _reverse_lookup(
            capabilities.assistant_text_policy,
            _ASSISTANT_TEXT_POLICIES,
            "assistant_text_policy",
        ),
    }


def resolve_effective_preset(model_name: str, provider: str = "") -> str:
    """Return the preset name that matched *model_name* / *provider*.

    Mirrors the first-match-wins routing in :func:`_match_preset` but returns
    only the preset identifier, never the merged config. If no ``presets:``
    entry matched, returns ``"default"`` — the same label the orchestrator
    records in ``task.yaml.model_config.<role>.resolved.effective_preset``
    when a model falls through to the base-case defaults.

    Examples
    --------
    >>> resolve_effective_preset("anthropic/claude-opus-4.7")
    'anthropic_claude_4_7'
    >>> resolve_effective_preset("openai/gpt-5.5")
    'openai_gpt5'
    >>> resolve_effective_preset("some-unknown-model")
    'default'
    """
    for preset_name, _preset in _iter_preset_matches(model_name, provider):
        return preset_name  # first match wins
    return "default"
