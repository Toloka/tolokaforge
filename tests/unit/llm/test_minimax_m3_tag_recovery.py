"""MiniMax-M3 ``tags`` recovery — recursive, tags-site-scoped response policies.

MiniMax-M3 routes its tool calls through a provider-side XML -> JSON conversion
that corrupts the ``tags`` argument on every emission (2505/2505 airlines
occurrences). Two dominant shapes are recovered:

* ``{"item": X}`` — the XML repeated-element artefact (1901/2505 ≈ 76 %),
  unwrapped to a list by :class:`ItemRecursiveUnwrapResponse`.
* a JSON-encoded list string (``'["a"]'``) or empty string (``''``)
  (577/2505 ≈ 23 %), recovered by :class:`JsonRecursiveCoerceResponse`.

The composite :class:`MinimaxM3TagRecoveryResponse` chains both and is wired
to the ``minimax`` preset (``minimax-m3*``) only.

Contract pinned here:

1. ``{"item": X}`` unwraps to a list (scalar -> ``[scalar]``, list kept flat,
   nested ``{"item": {"item": X}}`` flattens recursively).
2. Stringified JSON list -> native list; ``''`` -> ``[]`` AT THE TAGS SITE ONLY.
3. A scalar string is NEVER promoted; ``None`` is NEVER touched.
4. An already-valid ``list[str]`` passes through unchanged (zero false positives).
5. A multi-key dict at a tags site is left unchanged (no guessing).
6. The same shapes on a NON-tags field are left unchanged (scoping proof).
7. The ``minimax`` preset resolves to the composite.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import (
    ItemRecursiveUnwrapResponse,
    JsonRecursiveCoerceResponse,
    MinimaxM3TagRecoveryResponse,
    build_capabilities,
)
from tolokaforge.core.llm.presets import resolve_effective_preset, resolve_policy_names

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: the two real M3 wire shapes
# ---------------------------------------------------------------------------


def _update_call(tags: object) -> dict[str, object]:
    """A ``zendesk_update_item`` argument set with ``tags`` under ``updates``."""
    return {
        "table": "tickets",
        "id": "TCK-00000004",
        "updates": {"status": "solved", "tags": tags, "resolution_outcome": "Resolved"},
    }


def _create_call(tags: object) -> dict[str, object]:
    """A ``zendesk_create_item`` argument set with ``tags`` under ``item``."""
    return {
        "table": "tickets",
        "item": {"subject": "Proof of travel", "status": "open", "tags": tags},
    }


# ---------------------------------------------------------------------------
# ItemRecursiveUnwrapResponse — the {"item": X} XML artefact (76 %)
# ---------------------------------------------------------------------------


class TestItemRecursiveUnwrap:
    def test_item_wrap_scalar_to_single_element_list(self) -> None:
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call({"item": "receipt-issued"}))
        assert out["updates"]["tags"] == ["receipt-issued"]

    def test_item_wrap_list_kept_flat(self) -> None:
        """``{"item": [..]}`` returns the inner list as-is, not ``[[..]]``."""
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call({"item": ["a", "b"]}))
        assert out["updates"]["tags"] == ["a", "b"]

    def test_recursive_double_item_wrap_flattens(self) -> None:
        """``{"item": {"item": "a"}}`` recurses first, yielding flat ``["a"]``."""
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call({"item": {"item": "a"}}))
        assert out["updates"]["tags"] == ["a"]

    def test_item_wrap_on_item_parent(self) -> None:
        """The ``item.tags`` site (zendesk_create_item) is recovered too."""
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_create_call({"item": "vip"}))
        assert out["item"]["tags"] == ["vip"]

    def test_multikey_dict_left_unchanged(self) -> None:
        """No safe way to guess the value key -> refuse, leave as-is."""
        policy = ItemRecursiveUnwrapResponse()
        corrupt = {"item": "cancellation-executed", "refund-requested": ""}
        out = policy.parse_arguments(_update_call(dict(corrupt)))
        assert out["updates"]["tags"] == corrupt

    def test_valid_list_passthrough(self) -> None:
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call(["receipt-issued", "vip"]))
        assert out["updates"]["tags"] == ["receipt-issued", "vip"]

    def test_none_untouched(self) -> None:
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call(None))
        assert out["updates"]["tags"] is None

    def test_deep_nesting_capped_no_recursion_error(self) -> None:
        """Pathological ``{"item": {"item": ...}}`` nesting beyond the depth cap
        is returned without fully unwrapping, instead of raising RecursionError
        (which un-capped recursion would at this depth)."""
        deep: object = "x"
        for _ in range(2000):
            deep = {"item": deep}
        policy = ItemRecursiveUnwrapResponse()
        out = policy.parse_arguments(_update_call(deep))  # must not raise
        assert isinstance(out["updates"]["tags"], list)


# ---------------------------------------------------------------------------
# JsonRecursiveCoerceResponse — stringified list + empty string (23 %)
# ---------------------------------------------------------------------------


class TestJsonRecursiveCoerce:
    def test_stringified_list_to_real_list(self) -> None:
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_update_call('["receipt-issued"]'))
        assert out["updates"]["tags"] == ["receipt-issued"]

    def test_empty_string_to_empty_list_at_tags_site(self) -> None:
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_update_call(""))
        assert out["updates"]["tags"] == []

    def test_scalar_string_not_promoted(self) -> None:
        """A bare scalar tag string is left as-is (no guessing, no split)."""
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_update_call("escalated-relations"))
        assert out["updates"]["tags"] == "escalated-relations"

    def test_none_untouched(self) -> None:
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_update_call(None))
        assert out["updates"]["tags"] is None

    def test_valid_list_passthrough(self) -> None:
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_update_call(["a", "b"]))
        assert out["updates"]["tags"] == ["a", "b"]

    def test_empty_string_on_item_parent(self) -> None:
        policy = JsonRecursiveCoerceResponse()
        out = policy.parse_arguments(_create_call(""))
        assert out["item"]["tags"] == []


# ---------------------------------------------------------------------------
# Scoping — the same shapes on a NON-tags field must be untouched
# ---------------------------------------------------------------------------


class TestScoping:
    def test_empty_string_on_scalar_field_not_coerced(self) -> None:
        """The load-bearing guard: ``''`` on a non-tags scalar stays ``''``.

        A schema-agnostic empty-string -> [] was proven net-harmful (it
        corrupts scalar fields like ``resolution_category__c`` on M2.7). The
        ARRAY_SITES allowlist prevents the coercion from firing here.
        """
        policy = JsonRecursiveCoerceResponse()
        call = {
            "table": "tickets",
            "id": "TCK-1",
            "updates": {"resolution_category__c": "", "employee_id": "", "tags": ["ok"]},
        }
        out = policy.parse_arguments(call)
        assert out["updates"]["resolution_category__c"] == ""
        assert out["updates"]["employee_id"] == ""
        assert out["updates"]["tags"] == ["ok"]

    def test_item_wrap_on_non_tags_field_unchanged(self) -> None:
        """A ``{"item": X}`` shape on a non-tags field is NOT unwrapped."""
        policy = ItemRecursiveUnwrapResponse()
        call = {
            "table": "tickets",
            "id": "TCK-1",
            "updates": {"keyword": {"item": "x"}, "tags": {"item": "real"}},
        }
        out = policy.parse_arguments(call)
        assert out["updates"]["keyword"] == {"item": "x"}  # untouched (not a tags site)
        assert out["updates"]["tags"] == ["real"]  # tags site recovered

    def test_top_level_tags_not_under_known_parent_untouched(self) -> None:
        """``tags`` is only a site under ``updates`` / ``item`` parents."""
        policy = MinimaxM3TagRecoveryResponse()
        call = {"tags": {"item": "x"}, "other": {"item": "y"}}
        out = policy.parse_arguments(call)
        assert out == call


# ---------------------------------------------------------------------------
# MinimaxM3TagRecoveryResponse — composite (coerce then unwrap)
# ---------------------------------------------------------------------------


class TestComposite:
    def test_dict_item_shape_recovered(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call({"item": "receipt-issued"}))
        assert out["updates"]["tags"] == ["receipt-issued"]

    def test_stringified_list_recovered(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call('["receipt-issued"]'))
        assert out["updates"]["tags"] == ["receipt-issued"]

    def test_empty_string_recovered(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call(""))
        assert out["updates"]["tags"] == []

    def test_valid_list_passthrough_zero_false_positive(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call(["receipt-issued", "vip"]))
        assert out["updates"]["tags"] == ["receipt-issued", "vip"]

    def test_none_untouched(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call(None))
        assert out["updates"]["tags"] is None

    def test_multikey_dict_left_unchanged(self) -> None:
        policy = MinimaxM3TagRecoveryResponse()
        corrupt = {"item": "a", "b": ""}
        out = policy.parse_arguments(_update_call(dict(corrupt)))
        assert out["updates"]["tags"] == corrupt

    def test_other_args_untouched(self) -> None:
        """Recovery only rewrites the tags sites; sibling args are preserved."""
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call({"item": "vip"}))
        assert out["table"] == "tickets"
        assert out["id"] == "TCK-00000004"
        assert out["updates"]["status"] == "solved"
        assert out["updates"]["resolution_outcome"] == "Resolved"

    def test_composite_order_recovers_stringified_item_wrap(self) -> None:
        """coerce-before-unwrap is load-bearing: a stringified ``{"item": X}``
        is JSON-decoded to a dict first, then unwrapped to a list. The reverse
        order would leave it as a non-list ``{"item": X}`` dict."""
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call('{"item": "receipt-issued"}'))
        assert out["updates"]["tags"] == ["receipt-issued"]

    def test_list_elements_not_recursed(self) -> None:
        """Unwrap does not descend into list elements: a stringified list whose
        only element is itself an ``{"item": X}`` dict is left flat, not rewritten."""
        policy = MinimaxM3TagRecoveryResponse()
        out = policy.parse_arguments(_update_call('[{"item": "a"}]'))
        assert out["updates"]["tags"] == [{"item": "a"}]


# ---------------------------------------------------------------------------
# Preset wiring — minimax-m3* resolves to the composite, M2.7 does not
# ---------------------------------------------------------------------------


class TestPresetWiring:
    @pytest.mark.parametrize(
        "model",
        ["minimax/minimax-m3", "minimax/minimax-m3-1m", "openrouter/minimax-m3"],
    )
    def test_minimax_m3_resolves_to_composite(self, model: str) -> None:
        caps = build_capabilities(model, "openrouter")
        assert isinstance(caps.response_policy, MinimaxM3TagRecoveryResponse)
        assert resolve_effective_preset(model, "openrouter") == "minimax"
        assert resolve_policy_names(caps)["response_policy"] == "minimax_m3_tags"

    def test_minimax_m27_not_routed_to_composite(self) -> None:
        """M2.7 emits native ``tags`` lists (0 corrupt) and must NOT pick up
        the M3 recovery policy — it falls through to the default standard
        response (its real null-arg problem is owned by the schema sanitiser).
        """
        caps = build_capabilities("minimax/minimax-m2.7", "openrouter")
        assert not isinstance(caps.response_policy, MinimaxM3TagRecoveryResponse)
        assert resolve_effective_preset("minimax/minimax-m2.7", "openrouter") != "minimax"
