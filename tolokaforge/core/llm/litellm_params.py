"""Admitting the parameters a model accepts, when litellm's map has never heard of it.

See ``docs/LLM_LAYER.md`` § "When litellm has never heard of the model" for the
measured version behaviour and for the fixes that look right and are not.

litellm decides which OpenAI parameters a provider may be sent by looking the
model up in its own map. For most providers that decision is generic, but a
vendor-native one narrows it by the entry: measured on 1.96.0, the `meta` route
admits 32 parameters for a model the map carries and 26 for one it does not,
and the six it withholds are exactly ``function_call``, ``functions``,
``parallel_tool_calls``, ``reasoning_effort``, ``tool_choice`` and ``tools``.
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

logger = logging.getLogger(__name__)

#: Models whose evidence line has been logged. A client is built per trial per
#: role, so without this a 4000-trial eval repeats the same sentence thousands
#: of times. Nothing reads it but the logger.
_LOGGED: set[str] = set()

__all__ = ["DECLARABLE_FLAGS", "FLAG_PARAMS", "allowed_openai_params"]


#: Declared capability -> the OpenAI parameters it admits. Every flag here
#: admits something this engine actually sends: `tool_choice` is only ever set
#: alongside `tools`, and `parallel_tool_calls` is never set at all, so flags
#: for those would validate cleanly, admit a parameter no request carries, and
#: leave the run refused on the one it needed. Extending this map is a decision
#: about what we are willing to assert, and about what we actually send.
#:
#: ``supports_reasoning`` is here because a config that sets
#: ``models.agent.reasoning`` sends ``reasoning_effort``, which litellm refuses
#: for an unmapped model exactly as it refuses ``tools``.
FLAG_PARAMS: dict[str, tuple[str, ...]] = {
    "supports_function_calling": ("tools", "tool_choice"),
    "supports_reasoning": ("reasoning_effort",),
}

#: The flags an overlay entry may set, in declaration order.
DECLARABLE_FLAGS: tuple[str, ...] = tuple(FLAG_PARAMS)


def _entry_key(model_id: str, provider: str) -> str:
    """The overlay key for a model id litellm will be asked about.

    Overlay keys are always ``<provider>/<model>`` - one shape to validate and
    one to document - but the id litellm resolves is not always: the Nova path
    sends a bare name. Composing the key from the provider keeps a config whose
    model id carries no vendor reachable from the overlay.

    The vendor is lowercased on both sides of this lookup (see
    ``presets._validate_litellm_models``), so a config and an overlay that
    disagree on the case of ``Meta`` still meet. A validator that accepted what
    the lookup could not find would produce the one report nobody can act on:
    the overlay is loaded and the model still refuses tools.
    """
    if "/" in model_id:
        vendor, _, name = model_id.partition("/")
        return f"{vendor.lower()}/{name}"
    vendor = (provider or "").strip().lower()
    return f"{vendor}/{model_id}" if vendor else model_id


def allowed_openai_params(model_id: str, provider: str = "") -> list[str]:
    """Parameters an overlay entry admits for *model_id*, for litellm's kwarg.

    Empty when no entry declares this model, which is every model litellm
    already knows - the kwarg is then omitted and nothing about the request
    changes.

    *model_id* is the string litellm resolves (``_format_model_name``);
    *provider* names the vendor for the ids that do not carry one.
    """
    from tolokaforge.core.llm.presets import litellm_model_entries

    entry = litellm_model_entries().get(_entry_key(model_id, provider))
    if not entry:
        return []

    params: list[str] = []
    for flag, names in FLAG_PARAMS.items():
        if not entry.get(flag):
            continue
        params.extend(name for name in names if name not in params)
    if params and model_id not in _LOGGED:
        _LOGGED.add(model_id)
        logger.info(
            "Admitting %s for %s, which litellm's map does not carry. %s",
            ", ".join(params),
            model_id,
            entry.get("evidence"),
        )
    return params
