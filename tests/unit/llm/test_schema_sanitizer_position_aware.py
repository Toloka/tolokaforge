"""Position-aware sanitizer contract — guards three classes of silent corruption.

Each test class corresponds to one bug observed in the post-PR-#88 production
run (see :file:`plans/eval_post_pr88_schema_sanitizer_diagnosis.md`).

* :class:`TestPropertyNamedTitlePreserved` — :class:`StrictSchema` must NOT
  delete a property whose name happens to collide with a JSON-Schema metadata
  key (``title``, ``examples``, ``format``, …). Pre-fix, the recursive walker
  iterated ``properties: {title: {...}}`` and stripped the ``title`` entry as
  if it were the metadata keyword. Result: the on-disk persisted schema for
  ``d365_api_create_case`` declared ``required: [account_id, title,
  description]`` while ``properties`` no longer contained ``title`` — every
  call returned ``Input validation failed: title: Field required``.
* :class:`TestFreeFormObjectPropertyPreservesShapeHints` — a property of shape
  ``{type:object, additionalProperties:true, examples:[…]}`` is the
  free-form-record idiom (Pydantic emits this for ``BaseModel`` fields typed
  as ``dict``-with-arbitrary-keys). Pre-fix, ``additionalProperties: true``
  AND ``examples`` were both stripped, leaving a bare ``{type:object}`` with
  no shape hint. Result: GPT-5.5 alternated between omitting the field,
  flat-packing inner fields at the parent level, and (rarely) the right
  shape — across the ots_19_airlines run, **999/1000 trials hit a schema
  validation error**.
* :class:`TestPrimitiveStringExamplesPreserved` — for primitive properties
  (``{type:string, examples:["…"]}``) the ``examples`` value is often the
  ONLY format hint after ``pattern`` and ``format`` are stripped. Pre-fix,
  ``examples`` were stripped recursively, so the model lost the only signal
  about non-obvious string shapes (e.g. ``pay_period`` underscores). Result:
  438 ``Invalid or malformed pay_period`` errors on logistics.
* :class:`TestRequiredSubsetOfPropertiesInvariant` — the structural invariant
  every JSON-Schema sanitiser must uphold. Loud :class:`ValueError` if any
  ``required`` entry refers to a missing property. This is the regression
  guard that would have caught Bug 1 in CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.schema_sanitizer import StrictSchema

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap(name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": parameters,
        },
    }


def _params(tool: dict[str, Any]) -> dict[str, Any]:
    return tool["function"]["parameters"]


# ---------------------------------------------------------------------------
# Bug 1 — property whose name collides with a metadata keyword
# ---------------------------------------------------------------------------


class TestPropertyNamedTitlePreserved:
    """Property names are *opaque strings*, not JSON-Schema metadata keys.

    A property literally named ``title`` (or ``examples``, ``format``, …)
    must round-trip through the sanitiser unchanged.
    """

    @pytest.fixture
    def create_case_tool(self) -> dict[str, Any]:
        # Mirrors the real ots_bank_hr_d365 / ots_travel_marketplace
        # ``create_case`` shape: account_id + title + description required,
        # title carries Pydantic's per-field ``title``+``examples`` metadata.
        return _wrap(
            "create_case",
            {
                "type": "object",
                "required": ["account_id", "title", "description"],
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Account identifier.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short free-text title.",
                        "examples": ["Refund request for cancelled stay"],
                        "title": "Title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed free-text description.",
                    },
                },
            },
        )

    def test_property_named_title_survives_sanitization(
        self, create_case_tool: dict[str, Any]
    ) -> None:
        out = StrictSchema().sanitize([create_case_tool])
        props = _params(out[0])["properties"]
        assert "title" in props, (
            "Property named 'title' was deleted — sanitiser confused a "
            f"property name with a JSON-Schema metadata key. Properties: "
            f"{sorted(props.keys())}"
        )

    def test_required_keys_all_appear_in_properties(self, create_case_tool: dict[str, Any]) -> None:
        out = StrictSchema().sanitize([create_case_tool])
        params = _params(out[0])
        required = set(params["required"])
        prop_names = set(params["properties"].keys())
        missing = required - prop_names
        assert not missing, (
            f"required ⊄ properties — {sorted(missing)} are required but "
            f"not defined in properties. This means the schema is broken "
            f"and the model cannot satisfy it."
        )

    def test_property_named_examples_survives_sanitization(self) -> None:
        """Same bug class with a different metadata keyword."""
        tool = _wrap(
            "annotate",
            {
                "type": "object",
                "required": ["target", "examples"],
                "properties": {
                    "target": {"type": "string"},
                    "examples": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Examples to annotate.",
                    },
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        props = _params(out[0])["properties"]
        assert "examples" in props
        assert props["examples"]["type"] == "array"

    def test_property_named_format_survives_sanitization(self) -> None:
        """Same bug class — 'format' is also a JSON-Schema keyword."""
        tool = _wrap(
            "render",
            {
                "type": "object",
                "required": ["payload", "format"],
                "properties": {
                    "payload": {"type": "string"},
                    "format": {
                        "type": "string",
                        "enum": ["pdf", "html", "json"],
                    },
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        props = _params(out[0])["properties"]
        assert "format" in props
        assert props["format"]["type"] == "string"
        assert "enum" in props["format"]


# ---------------------------------------------------------------------------
# Bug 2 — free-form-object property keeps shape hints
# ---------------------------------------------------------------------------


class TestFreeFormObjectPropertyPreservesShapeHints:
    """Free-form ``{type:object, additionalProperties:true, examples:[…]}``
    properties are common (Pydantic ``BaseModel`` ``dict``-typed fields).

    The sanitiser must not reduce them to bare ``{type:object}`` — that
    leaves the model with no signal about the inner shape and triggers the
    flat-pack / omit failure cascade observed on ots_19_airlines.
    """

    @pytest.fixture
    def create_item_tool(self) -> dict[str, Any]:
        # Mirrors the real ots_19_airlines ``zendesk_create_item`` shape.
        return _wrap(
            "zendesk_create_item",
            {
                "type": "object",
                "required": ["table", "item"],
                "properties": {
                    "table": {
                        "type": "string",
                        "enum": ["tickets", "users"],
                    },
                    "item": {
                        "type": "object",
                        "additionalProperties": True,
                        "examples": [
                            {
                                "subject": "Refund request",
                                "description": "Customer requests refund.",
                                "status": "new",
                            }
                        ],
                        "title": "Item",
                        "description": "The record to create.",
                    },
                },
            },
        )

    def test_additional_properties_true_survives(self, create_item_tool: dict[str, Any]) -> None:
        out = StrictSchema().sanitize([create_item_tool])
        item = _params(out[0])["properties"]["item"]
        assert item.get("additionalProperties") is True, (
            "additionalProperties:true is the free-form-object marker — it "
            f"must be preserved. Got: {item!r}"
        )

    def test_examples_survive_on_free_form_object(self, create_item_tool: dict[str, Any]) -> None:
        out = StrictSchema().sanitize([create_item_tool])
        item = _params(out[0])["properties"]["item"]
        assert "examples" in item, (
            "examples are the only shape hint a free-form object carries — "
            f"sanitiser must preserve them. Got: {item!r}"
        )
        assert isinstance(item["examples"], list)
        assert isinstance(item["examples"][0], dict)
        assert "subject" in item["examples"][0]


# ---------------------------------------------------------------------------
# Bug 3 — `examples` on primitive string properties
# ---------------------------------------------------------------------------


class TestPrimitiveStringExamplesPreserved:
    """When a string property has only ``examples`` as its formatting hint
    (``pattern`` / ``format`` may be absent or stripped for RE2 reasons),
    the sanitiser must preserve ``examples`` so the model has SOMETHING to
    anchor on. This is the logistics ``pay_period`` regression."""

    def test_examples_preserved_on_primitive_string(self) -> None:
        tool = _wrap(
            "get_timecard",
            {
                "type": "object",
                "required": ["pay_period"],
                "properties": {
                    "pay_period": {
                        "type": "string",
                        "description": "Pay period",
                        "examples": ["2026-02-15_2026-02-28"],
                        "title": "Pay Period",
                    }
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        pp = _params(out[0])["properties"]["pay_period"]
        assert "examples" in pp, f"examples must survive on primitive strings; got {pp!r}"
        assert pp["examples"] == ["2026-02-15_2026-02-28"]


# ---------------------------------------------------------------------------
# Bug 4 — date-time format hint preserved (not over-stripped)
# ---------------------------------------------------------------------------


class TestStandardFormatHintsPreserved:
    """``format: date-time`` is accepted by every current provider's tool-call
    validator; stripping it is over-aggressive and removes a useful hint.

    Only RE2-incompatible *patterns* must be stripped — ``format`` is
    informational metadata, not regex.
    """

    def test_date_time_format_survives(self) -> None:
        tool = _wrap(
            "schedule",
            {
                "type": "object",
                "required": ["when"],
                "properties": {
                    "when": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Event time.",
                    }
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        when = _params(out[0])["properties"]["when"]
        assert (
            when.get("format") == "date-time"
        ), f"format: date-time must be preserved; got {when!r}"

    def test_safe_pattern_survives(self) -> None:
        """A regular pattern with no lookarounds / backrefs is safe to send
        through to OpenAI's RE2 validator — the sanitiser must not strip it."""
        tool = _wrap(
            "create_sku",
            {
                "type": "object",
                "required": ["sku"],
                "properties": {
                    "sku": {
                        "type": "string",
                        "pattern": r"^SKU-[A-Z0-9]+$",
                        "description": "SKU identifier.",
                    }
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        sku = _params(out[0])["properties"]["sku"]
        assert sku.get("pattern") == r"^SKU-[A-Z0-9]+$", (
            "RE2-safe pattern must be preserved; only lookaround/backref "
            f"patterns trip the strip. Got {sku!r}"
        )

    def test_re2_incompatible_pattern_still_stripped(self) -> None:
        """The Decimal-style negative-lookahead pattern stays the only thing
        we strip — and we strip it loudly, not silently."""
        tool = _wrap(
            "amount_check",
            {
                "type": "object",
                "required": ["amount"],
                "properties": {
                    "amount": {
                        "type": "string",
                        "pattern": r"^(?!^[-+.]*$)[-+]?\d+(\.\d+)?$",
                    }
                },
            },
        )
        out = StrictSchema().sanitize([tool])
        amount = _params(out[0])["properties"]["amount"]
        assert (
            "pattern" not in amount
        ), f"RE2-incompatible pattern must be stripped; got pattern={amount.get('pattern')!r}"


# ---------------------------------------------------------------------------
# Structural-invariant validator
# ---------------------------------------------------------------------------


class TestRequiredSubsetOfPropertiesInvariant:
    """The sanitiser fails LOUDLY when a sanitised schema would mislead the
    model — specifically when ``required`` references a property that the
    output's ``properties`` does not declare.

    This is the regression guard for Bug 1: any future code path that drops
    a property without dropping it from ``required`` raises here, instead of
    shipping a broken schema to the provider.
    """

    def test_input_with_inconsistent_required_raises(self) -> None:
        """The sanitiser refuses an input where ``required`` lists a name
        not in ``properties`` — fail-early surfaces upstream schema bugs
        too, not just our own."""
        bad = _wrap(
            "broken",
            {
                "type": "object",
                "required": ["a", "b"],
                "properties": {"a": {"type": "string"}},
            },
        )
        with pytest.raises(ValueError, match=r"required.*properties"):
            StrictSchema().sanitize([bad])

    def test_nested_object_invariant_checked(self) -> None:
        """The validator walks into nested object schemas — a broken
        ``items`` schema raises."""
        bad = _wrap(
            "wrap_array",
            {
                "type": "object",
                "required": ["items_list"],
                "properties": {
                    "items_list": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["k1", "k2"],
                            "properties": {"k1": {"type": "string"}},
                        },
                    }
                },
            },
        )
        with pytest.raises(ValueError, match=r"required.*properties"):
            StrictSchema().sanitize([bad])

    def test_well_formed_input_does_not_raise(self) -> None:
        ok = _wrap(
            "ok",
            {
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "string"}},
            },
        )
        StrictSchema().sanitize([ok])  # no raise
