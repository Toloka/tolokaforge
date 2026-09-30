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
from dataclasses import dataclass

from tolokaforge.core.llm.providers import litellm_model_id

logger = logging.getLogger(__name__)

#: Overlay keys whose evidence line has been logged. A client is built per trial per
#: role, so without this a 4000-trial eval repeats the same sentence thousands
#: of times. Nothing reads it but the logger.
_LOGGED: set[str] = set()

__all__ = [
    "DECLARABLE_FLAGS",
    "FLAG_PARAMS",
    "OverlayLookup",
    "allowed_openai_params",
    "lookup_overlay",
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
    """The overlay entry a config resolves to, and what that entry admits."""

    key: str
    params: tuple[str, ...]


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
        vendor, _, rest = model_id.partition("/")
        return f"{vendor.lower()}/{rest}"
    return f"{provider.lower()}/{model_id}"


def lookup_overlay(provider: str, name: str) -> OverlayLookup:
    """The overlay entry for ``provider`` and ``name`` as the config states them.

    ``config validate`` and :class:`LLMClient` both ask this, so the preflight
    and the run cannot disagree about which entry applies. ``params`` is empty
    when no entry declares the model, which is every model litellm already
    knows.
    """
    from tolokaforge.core.llm.presets import litellm_model_entries

    key = _overlay_key(provider, name)
    entry = litellm_model_entries().get(key)
    if not entry:
        return OverlayLookup(key=key, params=())

    params: list[str] = []
    for flag, names in FLAG_PARAMS.items():
        if not entry.get(flag):
            continue
        params.extend(param for param in names if param not in params)
    return OverlayLookup(key=key, params=tuple(params))


def allowed_openai_params(provider: str, name: str) -> list[str]:
    """Parameters an overlay entry admits for a config, for litellm's kwarg.

    Empty when no entry declares the model; the kwarg is then omitted and
    nothing about the request changes. Logs the entry's evidence once per key.
    """
    lookup = lookup_overlay(provider, name)
    if lookup.params and lookup.key not in _LOGGED:
        from tolokaforge.core.llm.presets import litellm_model_entries

        _LOGGED.add(lookup.key)
        logger.info(
            "Admitting %s for %s, which litellm's map does not carry. %s",
            ", ".join(lookup.params),
            lookup.key,
            litellm_model_entries()[lookup.key].get("evidence"),
        )
    return list(lookup.params)
