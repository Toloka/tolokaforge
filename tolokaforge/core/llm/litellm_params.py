"""Admitting the parameters a model accepts, when litellm's map has never heard of it.

See ``docs/LLM_LAYER.md`` § "When litellm has never heard of the model" for the
measured version behaviour and for the fixes that look right and are not.

litellm decides which OpenAI parameters a provider may be sent by looking the
model up in its own map. For most providers that decision is generic, but a
vendor-native one narrows it by the entry: measured on 1.96.0, the `meta` route
admits 32 parameters for a model the map carries and 26 for one it does not,
and the six it withholds are exactly ``function_call``, ``functions``,
``parallel_tool_calls``, ``reasoning_effort``, ``tool_choice`` and ``tools`` —
the last four of which this engine emits when a config asks for them.
Temperature, max_tokens, top_p, seed and the rest pass untouched - which is why
the error names only the tool parameters, and why it is rejected before any
request leaves the process::

    litellm.UnsupportedParamsError: meta does not support parameters:
    ['tools', 'tool_choice'], for model=muse-spark-1.2

That is a gap in litellm's data, not a statement about the model - the same
model returns a correct tool call once the parameters are admitted.

litellm's own answer to this is ``allowed_openai_params``, a per-call kwarg
naming the parameters to admit past the map gating for that one request; its
error message says so. This module turns an operator's declaration into that
list. It carries no list of models: a model missing from a third-party map is
not a fact about this engine release, and pinning one here would tie every
future gap to the release cadence - the same argument ADR 0002 made for preset
data. So entries are operator data, declared in the preset overlay
(``--presets-file`` / ``RunConfig.engine.presets_file``)::

    litellm_models:
      meta/muse-spark-1.2:
        supports_function_calling: true
        supports_reasoning: true      # the config sets models.agent.reasoning
        evidence: "2026-08-10, litellm 1.96.0: no entry, so meta refused tools
          before sending; admitting them returns a correct tool call."

The key is the config's ``<provider>/<name>``, ``name`` verbatim, slashes
included (``openai/self-hosted/qwen3.6-35b-a3b``): :func:`lookup_overlay` derives
it from :func:`~tolokaforge.core.llm.providers.litellm_model_id`, the string the
run sends, and ``config validate`` asks the same function.

An entry DECLARES; it does not copy. Only the parameters its flags name are
admitted, so a capability nothing observed is never asserted on the model's
behalf, and an undeclared parameter is still refused loudly - the allow-list
only ever ADDS to what litellm already permits.

Nothing is written into litellm's global model map. That keeps three problems
from existing: a price of ours cannot end up labelled ``cost_source="litellm"``
(provider-authoritative) when it is our own table, an entry cannot survive to
overwrite the richer upstream row once litellm ships one, and there is no
process-global mutation to synchronise across the trial thread pool. When
upstream does ship the entry, the allow-list becomes a harmless no-op.

Never reach for ``drop_params`` to silence the error this fixes: that strips
``tools`` and turns every tool-use trial into a no-tool trial, which reads as a
capability result rather than a configuration one. Nor ``extra_body``, which
passes ungated: a provider silently ignoring a key smuggled through it is
invisible, which is the same failure wearing a different hat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tolokaforge.core.llm.providers import litellm_model_id, provider_binding_names

if TYPE_CHECKING:
    from tolokaforge.core.models import ModelConfig

logger = logging.getLogger(__name__)

#: Overlay keys whose evidence line has been logged. A client is built per trial per
#: role, so without this a 4000-trial eval repeats the same sentence thousands
#: of times. Nothing reads it but the logger.
_LOGGED: set[str] = set()

__all__ = [
    "DECLARABLE_FLAGS",
    "FLAG_PARAMS",
    "OverlayKeyMismatchError",
    "OverlayLookup",
    "allowed_openai_params",
    "lookup_overlay",
    "overlay_key_mismatches",
    "overlay_stray_entries",
]


#: Declared capability -> the OpenAI parameters it admits. Every flag here
#: admits something this engine actually sends: ``tool_choice`` is only ever
#: set alongside ``tools``, and ``parallel_tool_calls`` is sent when a
#: :class:`ModelConfig` sets it (again alongside ``tools``). Extending this
#: map is a decision about what we are willing to assert, and about what we
#: actually send.
#:
#: ``supports_reasoning`` is here because a config that sets
#: ``models.agent.reasoning`` sends ``reasoning_effort``, which litellm refuses
#: for an unmapped model exactly as it refuses ``tools``.
FLAG_PARAMS: dict[str, tuple[str, ...]] = {
    "supports_function_calling": ("tools", "tool_choice", "parallel_tool_calls"),
    "supports_reasoning": ("reasoning_effort",),
}

#: The flags an overlay entry may set, in declaration order.
DECLARABLE_FLAGS: tuple[str, ...] = tuple(FLAG_PARAMS)


@dataclass(frozen=True)
class OverlayLookup:
    """The overlay entry a config resolves to, and what that entry admits.

    ``evidence`` is the entry's own, ``None`` when no entry sits under ``key``.
    ``stray_key`` names an entry stored under the config's raw ``name`` that
    is the canonical key of a different, provider-named config, so it does
    not apply here; ``None`` when there is no such entry.
    """

    key: str
    params: tuple[str, ...]
    evidence: str | None
    stray_key: str | None

    @property
    def stray_provider(self) -> str | None:
        """The provider whose config ``stray_key`` is the key of."""
        return _vendor(self.stray_key) if self.stray_key else None


class OverlayKeyMismatchError(ValueError):
    """An overlay entry sits under a config's raw name instead of its ``<provider>/<name>``."""

    def __init__(self, *, provider: str, name: str, declared_key: str, expected_key: str):
        self.provider = provider
        self.name = name
        self.declared_key = declared_key
        self.expected_key = expected_key
        super().__init__(
            f"litellm_models entry {declared_key!r} does not apply to "
            f"provider {provider!r}, name {name!r}: that config is looked up under "
            f"{expected_key!r}, so the entry admits nothing. "
            f"Rename the entry to {expected_key!r}."
        )

    def __reduce__(self):
        return _rebuild_mismatch, (self.provider, self.name, self.declared_key, self.expected_key)


def _rebuild_mismatch(
    provider: str, name: str, declared_key: str, expected_key: str
) -> OverlayKeyMismatchError:
    return OverlayKeyMismatchError(
        provider=provider, name=name, declared_key=declared_key, expected_key=expected_key
    )


def _vendor(key: str) -> str:
    return key.partition("/")[0]


def _lower_vendor(key: str) -> str:
    """*key* with its first ``/`` segment lowercased, as the overlay validator stores it."""
    vendor, _, rest = key.partition("/")
    return f"{vendor.lower()}/{rest}"


def _overlay_key(provider: str, name: str) -> str:
    """The ``litellm_models`` key for a config's ``provider`` and ``name``.

    Derived from :func:`litellm_model_id`, the string the run sends, so the
    key for ``(openai, self-hosted/m)`` is ``openai/self-hosted/m``, never the
    raw ``self-hosted/m``. Overlay keys are always ``<provider>/<model>`` - one
    shape to validate and one to document - but the Nova id is bare, so a bare
    id is keyed under the provider.

    The vendor is lowercased on both sides of this lookup (see
    ``presets._validate_litellm_models``), so a config and an overlay that
    disagree on the case of ``Meta`` still meet. A validator that accepted what
    the lookup could not find would produce the one report nobody can act on:
    the overlay is loaded and the model still refuses tools.
    """
    model_id = litellm_model_id(provider, name)
    if "/" in model_id:
        return _lower_vendor(model_id)
    return f"{provider.lower()}/{model_id}"


def _raw_name_key(provider: str, name: str) -> str | None:
    """The key a config's raw ``name`` would be stored under, when it differs from its own.

    Only a name carrying a ``/`` that is not ``<provider>/`` has one; the vendor
    segment is lowercased as the overlay validator lowercases it.
    """
    if "/" not in name or name.startswith(f"{provider}/"):
        return None
    return _lower_vendor(name)


def _names_a_provider(key: str) -> bool:
    """Whether *key*'s first segment is a provider, so the key can be another config's own."""
    import litellm

    vendor = _vendor(key)
    return vendor in provider_binding_names() or vendor in litellm.provider_list


def lookup_overlay(provider: str, name: str) -> OverlayLookup:
    """The overlay entry for ``provider`` and ``name`` as the config states them.

    ``config validate`` and :class:`LLMClient` both ask this, so the preflight
    and the run cannot disagree about which entry applies. ``params`` is empty
    when no entry declares the model, which is every model litellm already
    knows.

    Raises :class:`OverlayKeyMismatchError` when the only entry for the config
    sits under its raw ``name`` and that key's first segment names no provider,
    so it can be no other config's key either (``self-hosted/m`` for
    ``(openai, self-hosted/m)``). A raw key that names a provider
    (``anthropic/m`` for ``(openrouter, anthropic/m)``) is the key of the native
    ``(anthropic, m)`` config and comes back as ``stray_key``.
    """
    from tolokaforge.core.llm.presets import litellm_model_entries

    entries = litellm_model_entries()
    key = _overlay_key(provider, name)
    entry = entries.get(key)
    if entry:
        return OverlayLookup(
            key=key, params=_admitted_params(entry), evidence=entry["evidence"], stray_key=None
        )

    raw_key = _raw_name_key(provider, name)
    if raw_key is None or raw_key not in entries:
        return OverlayLookup(key=key, params=(), evidence=None, stray_key=None)
    if not _names_a_provider(raw_key):
        raise OverlayKeyMismatchError(
            provider=provider, name=name, declared_key=raw_key, expected_key=key
        )
    return OverlayLookup(key=key, params=(), evidence=None, stray_key=raw_key)


def _admitted_params(entry: Mapping[str, object]) -> tuple[str, ...]:
    params: list[str] = []
    for flag, names in FLAG_PARAMS.items():
        if not entry.get(flag):
            continue
        params.extend(param for param in names if param not in params)
    return tuple(params)


def _lookup_each(
    models: Mapping[str, ModelConfig],
) -> Iterator[tuple[str, OverlayLookup | OverlayKeyMismatchError]]:
    """Every model config, fallbacks included, with its path and lookup outcome."""
    from tolokaforge.core.models.run_config import iter_model_configs

    for path, cfg in iter_model_configs(models):
        try:
            yield path, lookup_overlay(cfg.provider, cfg.name)
        except OverlayKeyMismatchError as err:
            yield path, err


def overlay_key_mismatches(
    models: Mapping[str, ModelConfig],
) -> list[tuple[str, OverlayKeyMismatchError]]:
    """Every model config, fallbacks included, whose overlay entry sits under its raw name.

    Pairs each refusal with the config path it came from (``models.agent``,
    ``models.agent.fallbacks[0]``). ``config validate`` reports all of them and
    ``run`` / ``prepare`` / ``worker`` raise the first, so both refuse the same
    configs.
    """
    return [
        (path, outcome)
        for path, outcome in _lookup_each(models)
        if isinstance(outcome, OverlayKeyMismatchError)
    ]


def overlay_stray_entries(models: Mapping[str, ModelConfig]) -> list[tuple[str, OverlayLookup]]:
    """Every model config, fallbacks included, whose raw name keys another config's entry."""
    return [
        (path, outcome)
        for path, outcome in _lookup_each(models)
        if isinstance(outcome, OverlayLookup) and outcome.stray_key
    ]


def allowed_openai_params(provider: str, name: str) -> list[str]:
    """Parameters an overlay entry admits for a config, for litellm's kwarg.

    Empty when no entry declares the model; the kwarg is then omitted and
    nothing about the request changes. Logs the entry's evidence once per key.
    """
    lookup = lookup_overlay(provider, name)
    if lookup.params and lookup.key not in _LOGGED:
        _LOGGED.add(lookup.key)
        logger.info(
            "Admitting %s for %s, which litellm's map does not carry. %s",
            ", ".join(lookup.params),
            lookup.key,
            lookup.evidence,
        )
    return list(lookup.params)
