"""Per-model policy subclasses for the xAI Grok family over OpenRouter.

One class ships in this module:

* :class:`XaiGrokRecursiveSchema` — ``strict``-flavoured sanitiser that adds
  cyclic-``$ref`` tolerance, scalar dict-map value carriage, and ``oneOf`` +
  ``discriminator`` flattening, while keeping ``StrictSchema``'s
  description / RE2-pattern stripping (unlike the Gemini lineage it borrows
  the three algorithms from).

Registered with the engine via the ``tolokaforge.policies`` entry-point
group (see :mod:`tolokaforge.core.model_data.load_policy_registrations`).

REFUTED, do not re-add — a ``DictMapHints`` subclass appending a
"emit tool calls on the native ``tool_calls`` channel, never as a fenced
JSON block" directive to the system prompt. Resolve iteration 2 shipped it
against the residual ``tool_calls == []`` failures and the controlled
reprobe measured it **harmful**: on the identical three probes the pass
count fell 10/15 -> 3/15 (``recursive_ref[nested_in_object]`` 3/5 -> 0/5,
``recursive_ref[simple]`` 3/5 -> 2/5, ``variant_dict_map[nested_in_object]``
4/5 -> 1/5). The premise was wrong: those residual failures are Grok's
intrinsic ~10 % no-tool-call rate — the same rate that depresses probes with
no schema surface at all (``simple_tool_call`` 14/15,
``heterogeneous_array`` 10-13/15) and whose one captured payload is *prose*
(``text='call tool calculate with expression is 250 * 12.50 + 500'``), not a
call envelope. Naming the fenced-JSON shape in the prompt appears to have
taught it as a template. See ``observation/resolve/decision.json``.

REFUTED, do not re-try — ``reasoning_codec: gemini`` on this route. Resolve
iteration 4 swapped that axis (following the ``qwen3.8-max`` precedent, where
``openai`` -> ``gemini`` bought unsigned replay) and the controlled reprobe
measured it CATASTROPHIC: all four reprobed targets fell 5/5 -> 0/5 with
``ValueError: Unknown Gemini reasoning_details type: 'reasoning.summary';
expected 'reasoning.text' or 'reasoning.encrypted'``, 15/15 identical.
Grok-4.6 labels its ``reasoning_details`` entries ``reasoning.summary``, a
type ``GeminiReasoningCodec``'s block decoder *raises* on rather than
skipping, so the codec aborts while PARSING every response — before any probe
assertion runs. That wipeout is an engine-side exception, not a model
regression, and it is why iteration 5 reverted to iteration 3's composition.
The qwen precedent does not transfer: qwen3.8-max emits ``summary_text``
blocks the Gemini codec accepts. The working codec on this route is
``openai_summary_replay``, whose inherited ``extract`` reads the flat
``reasoning`` / ``reasoning_content`` summary string and never walks the
``reasoning_details`` envelope, making it immune to the entry type.
"""

from __future__ import annotations

from tolokaforge_models.policies.gemini import GeminiRecursiveSchema

__all__ = ["XaiGrokRecursiveSchema"]


class XaiGrokRecursiveSchema(GeminiRecursiveSchema):
    """``StrictSchema`` + cyclic-``$ref`` tolerance + scalar dict-map carriage
    + ``oneOf``/``discriminator`` flattening, with Grok's stripping intact.

    Motivation — ``x-ai/grok-4.6`` over OpenRouter, observe run 2026-08-14.
    The bundled ``xai_grok`` preset routes through the plain ``strict``
    sanitiser, which loses declared structure on three surfaces. All three
    losses are *sanitiser-side* (the model never sees the affected fields), and
    all three are already solved by :class:`GeminiRecursiveSchema`, so this
    class **inherits every algorithm verbatim** and changes only the two
    Gemini-specific stripping flags:

    1. **Cyclic ``$ref``** — ``test_recursive_ref_tool_call`` failed 0/15 on all
       four shapes (simple / deep_chain / wide_tree / nested_in_object) with the
       in-engine ``"$ref resolution exceeded depth 16"`` ``ValueError``. The
       raise fires before the request is sent, so the trial dies without Grok
       ever receiving the tool. Inherited
       :meth:`GeminiRecursiveSchema.inline_refs_in_tool` prunes a genuine cycle
       to a permissive open object, keeping the wire schema finite.
    2. **Scalar dict-map values** — ``dict_map__scalar_values`` failed 0/15
       (``{'SKU-A': {}, …}``: every value dropped). A ``Dict[str, int]`` value
       schema has no ``properties`` to lift onto the synthetic items object, so
       without the inherited ``carry_scalar_dict_map_value`` the wire items are
       a value-less ``{key}``. Reversed by ``scalar_array_dict_map``, which the
       preset entry pairs with this sanitiser.
    3. **Discriminated-union dict-map values** — ``union_in_dict_map`` failed
       0/15, the map flattening to ``{'t1': {}, 'c1': {}}`` with Grok packing
       the whole entity into the ``key`` string (``{"t1', "kind": "ticket"…``).
       Same root cause: a ``oneOf`` value schema carries its ``properties``
       per-branch, so nothing is lifted. The inherited
       ``flatten_oneof_discriminator`` unions the branch properties into one
       object first, and the dict-map conversion then lifts them normally.
       Gated on the ``discriminator`` keyword by the inherited
       :meth:`_is_discriminated_union`, so **bare** ``Union[A, B]`` (Pydantic
       ``anyOf``, no ``discriminator``) is left untouched — the shape behind the
       logistics regression that flag's docstring warns about, and the shape
       Grok's own ``bare_union`` probe exercises.

    **Superset of the ``strict`` policy it replaces.** The two Gemini flags are
    reset to :class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema`'s
    defaults, so description / RE2-pattern handling on this route is unchanged
    from the ``xai_grok`` preset's ``strict`` behaviour. Both Gemini values are
    tuning for *Gemini's* observed regressions (a class-docstring that anchors
    Gemini's optional-field selection; RE2-incompatible patterns Gemini does not
    enforce), neither of which is evidenced on Grok — and Grok passes
    ``re2_pattern_tolerance`` 15/15 *with* the strip enabled, so keeping the
    inherited ``False`` would be an unmotivated change to a passing surface.
    Every other behaviour — ``$defs`` inlining, dict-map conversion, Decimal
    collapse, structural invariants — is the parent's, unmodified.
    """

    strip_parameters_root_description = True
    strip_re2_incompatible_patterns = True
