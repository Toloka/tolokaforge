"""Per-model policies for the Cohere Command family.

:class:`CohereRecursiveSchema` is the schema sanitiser: the *cyclic* ``$ref``
tolerance that :class:`GeminiRecursiveSchema` introduced, with none of the three
presentational relaxations that class carries for Gemini — one of which (keeping
RE2-incompatible ``pattern`` values) is actively fatal on this route.

A root-key repair also lived here and was REMOVED on review. This route sometimes
emits a single root argument key with the tool's own name glued onto it
(``submit_treeroot`` for the declared ``root``), and the repair renamed it back.
The gate it could express was far wider than the quirk: ``parse_arguments`` never
receives the tool name, so the check degraded to "the emitted key ends with the
declared parameter name", which also matches ``line_items`` for ``items`` and
``uuid`` for ``id``. That silently credits a model that emitted the WRONG
parameter name, which is a scoring change wearing a transport fix's clothes.
``recursive_ref_tool_call`` is therefore recorded as a genuine ceiling instead.

Registered with the engine via the ``tolokaforge.policies`` entry-point
group (see :mod:`tolokaforge.core.model_data.load_policy_registrations`).
"""

from __future__ import annotations

from tolokaforge_models.policies.gemini import GeminiRecursiveSchema

__all__ = ["CohereRecursiveSchema"]


class CohereRecursiveSchema(GeminiRecursiveSchema):
    """Cyclic-``$ref``-tolerant ``StrictSchema`` for the Cohere Command family.

    Inherits from :class:`GeminiRecursiveSchema` — **for its mechanism, not
    its Gemini tuning**. Two inherited behaviours are exactly what the Cohere
    observe run needs, and re-implementing either here would risk losing a
    documented recovery (see the parent classes for the authoritative
    descriptions):

    * **Cycle-tolerant ``$ref`` inlining** (``inline_refs_in_tool`` /
      ``_inline_refs_cycle_tolerant``, inherited verbatim). Every *non-cyclic*
      ``$ref`` inlines exactly as :class:`StrictSchema` does; a ref that
      re-enters a def already on the active resolution stack is replaced by
      ``{"type": "object", "additionalProperties": true}``. Cycle detection is
      by def-name on the active stack, so a diamond still inlines on each
      branch. Without it ``StrictSchema.inline_refs_in_tool`` raises
      ``"$ref resolution exceeded depth 16"`` *in-engine, before the request is
      sent* — observed on ``azure_ai/cohere-command-a-plus-05-2026`` as
      ``test_recursive_ref_tool_call`` failing 0/15 on all four shapes
      (simple / deep_chain / wide_tree / nested_in_object), every rep with that
      identical sanitiser ``ValueError``, i.e. the model never saw the tool.
    * **``carry_scalar_dict_map_value = True``** (inherited). A dict-map whose
      *value* schema has no ``properties`` to lift — a scalar ``Dict[str, int]``
      or a discriminated-union value — otherwise reaches the wire as
      ``items: {key}`` alone, dropping the value. Observed live on this model,
      0/15 each: ``dict_map__scalar_values`` packed the value into the key
      string (``{'SKU-A:10': {}}``) and ``discriminated_union__union_in_dict_map``
      emitted key-only items (``{'t1': {}, 'c1': {}}``, kinds ``{None}``).
      Pairs with ``response_policy: scalar_array_dict_map``, which reverses
      ``{key, value}`` back to the bare value.

    The three Gemini-specific relaxations are reset to their
    :class:`StrictSchema` defaults, which is what this model was measured under
    (26/31 capability probes passing on ``schema_sanitizer: strict``). Each
    reset is required by Cohere evidence, not by caution:

    * ``strip_re2_incompatible_patterns = True`` — **the load-bearing one.**
      The parent keeps lookaround patterns because Gemini treats them as inert
      format hints. This route does not: it compiles them and returns HTTP 500
      (``regex_converter.cc:75: Regex parsing error at position 4: Lookahead is
      not supported yet``) — the exact failure ``test_re2_pattern_tolerance``
      records 0/15, there via a deliberately forced passthrough sanitiser.
      Inheriting the parent's ``False`` would carry that 500 into every domain
      tool schema bearing a Pydantic Decimal-string pattern.
    * ``flatten_oneof_discriminator = False`` — Cohere handles ``oneOf`` +
      ``discriminator`` natively: ``discriminated_union_tool_call_two_turns``
      passed 15/15 on both ``bare_union`` and ``explicit_discriminator``, as
      did the ``array_of_unions`` and ``nested_union`` variants. Flattening is
      a workaround for a gap this model does not have, and it would reshape the
      union value schema now carried under the synthetic ``value`` field.
    * ``strip_parameters_root_description = True`` — the parent keeps the
      redundant Pydantic class-docstring at the parameters root because it
      anchors *Gemini's* optional-field selection. There is no such evidence
      on this route, and ``required_fields_complete`` already passes 15/15
      with it stripped.

    Firing condition and depth are therefore precisely
    :class:`GeminiRecursiveSchema`'s, unchanged: the cycle stub is substituted
    only where a ``#/$defs/`` ref re-enters a def on the current resolution
    path, and the scalar/union value carriage fires only where the sanitised
    dict-map value schema is non-empty and contributes no ``properties``. This
    class adds no new traversal and overrides no method — only the three
    ``ClassVar`` flags above.
    """

    flatten_oneof_discriminator = False
    strip_parameters_root_description = True
    strip_re2_incompatible_patterns = True
