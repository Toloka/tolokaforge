"""Per-model policies for the Cohere Command family.

Two classes ship in this module:

* :class:`CohereRecursiveSchema` — schema sanitiser combining the *cyclic*
  ``$ref`` tolerance that :class:`GeminiRecursiveSchema` introduced with
  none of the three presentational relaxations that class carries for
  Gemini — one of which (keeping RE2-incompatible ``pattern`` values) is
  actively fatal on this route.
* :class:`CohereRootKeyRepairResponse` — response policy renaming the
  tool-name-prefixed root argument key this route emits back to the
  declared parameter name.

Both ship here so the Cohere preset composes shipped behaviour instead of
re-deriving it.

Registered with the engine via the ``tolokaforge.policies`` entry-point
group (see :mod:`tolokaforge.core.model_data.load_policy_registrations`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tolokaforge_models.policies.gemini import (
    GeminiRecursiveSchema,
    ScalarArrayDictMapResponse,
)

__all__ = ["CohereRecursiveSchema", "CohereRootKeyRepairResponse"]


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


class CohereRootKeyRepairResponse(ScalarArrayDictMapResponse):
    """:class:`ScalarArrayDictMapResponse` plus a **root key rename**.

    Motivation — ``azure_ai/cohere-command-a-plus-05-2026``. On a tool whose
    arguments are a single ``object`` root parameter, this route sometimes
    emits that parameter's key with **the tool's own name prefixed onto it**,
    while the value underneath is entirely correct::

        # declared:  submit_tree({"root": {...}})
        # emitted:   {"submit_treeroot": {"label": "A", "children": [...]}}
        #                 ^^^^^^^^^^^ = "submit_tree" + "root"

    The receiving validator sees the declared parameter as simply absent, so
    the symptom presents as ``root`` being ``None`` — which is what an earlier
    iteration of this integration mis-read as a *dropped envelope*, and
    "restored" by re-nesting the whole mapping one level deeper (producing
    ``{"root": {"submit_treeroot": {...}}}``, the shape recorded in
    ``observation/resolve/reprobe_2``). That restore is what made the true
    mechanism visible: string concatenation, not elision.

    **The evidence, from this model's own observe artifacts.**

    * Iteration 1 (parent policy alone, no root recovery):
      ``test_recursive_ref_tool_call`` 0/5 on all three ``submit_tree``
      shapes, each reporting ``root`` as ``NoneType``.
    * Iteration 2 (envelope re-nest): still 0/5, but the assertion messages
      now print the arguments verbatim, and every one of the 10 failing
      ``submit_tree`` reports contains the literal key ``submit_treeroot``
      carrying a well-formed tree — ``simple`` reaches depth 3 with both
      ``B`` and ``C`` present, ``deep_chain`` reaches ``A→B→C→D``. The
      recursion the capability actually probes was never wrong.
    * ``nested_in_object`` passes 5/5 in both iterations. Its tool is
      ``submit_document`` with root parameter ``doc``, and no report in
      either run contains ``submit_documentdoc``. So the quirk is
      intermittent per tool rather than universal, which is why it is a
      repair keyed on an exact expected string and not a blanket rewrite.

    Because the mangled key is *derived from* the declared name, the repair is
    fully determined: there is exactly one candidate spelling to look for, and
    no guessing about which emitted key was "meant" to be the parameter.

    **Firing condition (schema-gated, depth 0 — a key rename only).** The
    rename fires only when ALL of the following hold:

    * ``param_types`` declares exactly ONE root parameter, and its declared
      type is ``object`` (the type comes from the sanitised schema, so the
      gate is schema-visible — never keyed on the data's shape);
    * that declared parameter name is ABSENT from the emitted arguments;
    * the emitted arguments contain exactly ONE key, and that key is
      **byte-equal to some tool name concatenated with the declared
      parameter name** — i.e. it ends with the declared name and the
      remaining prefix is non-empty. The prefix is required to be a
      non-empty proper prefix, so a correctly-spelled key can never match.

    On a match the single key is renamed to the declared parameter name and
    its value is passed through untouched. Nothing is re-nested, no value is
    rewritten, and no recursion is added: an argument set that already spells
    the parameter correctly, or that carries more than one key, or whose lone
    key is not the concatenated spelling, is returned unchanged and reaches
    the parent exactly as before.

    Everything else — empty-container coercion, stringified-JSON recovery,
    the dict-map array→dict pivot, the scalar ``value`` unwrap and the
    one-level nested dict-map pivot — is inherited from
    :class:`ScalarArrayDictMapResponse` and runs on the repaired arguments.
    That parent is the policy the earlier Cohere overlays already used (it is
    what took ``dict_map__scalar_values``, ``dict_map__nested_in_object`` and
    ``discriminated_union__union_in_dict_map`` from 0/15 to 5/5, and it
    round-trips a correct ``root`` tree unchanged), so inheriting rather than
    re-implementing keeps every one of those recoveries; this class adds only
    the rename above.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        repaired = self._repair_root_key(arguments, param_types)
        return super().parse_arguments(repaired, param_types=param_types)

    @staticmethod
    def _repair_root_key(
        arguments: dict[str, Any],
        param_types: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """Rename a ``<tool_name><param>`` root key back to ``<param>``.

        Schema-gated on ``param_types`` declaring exactly one root parameter
        of declared type ``object``; a no-op whenever that parameter is
        already present, whenever more than one key was emitted, or whenever
        the lone key is not the declared name carrying a non-empty prefix.
        See the class docstring for the full condition.
        """
        if not param_types or len(param_types) != 1:
            return arguments
        if not isinstance(arguments, dict) or len(arguments) != 1:
            return arguments
        ((param_name, declared),) = param_types.items()
        if declared != "object" or param_name in arguments:
            return arguments
        ((emitted_key, value),) = arguments.items()
        if not isinstance(emitted_key, str):
            return arguments
        # The mangled spelling is the declared name with a non-empty prefix
        # glued on (observed: the tool's own name). Requiring a *proper*
        # prefix means a correctly-spelled key never reaches here anyway.
        if len(emitted_key) <= len(param_name) or not emitted_key.endswith(param_name):
            return arguments
        return {param_name: value}
