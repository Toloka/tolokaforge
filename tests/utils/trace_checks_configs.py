"""One authored ``trace_checks`` block spanning the whole declared vocabulary.

Hand-written in the shape an author writes it, so the tests that read it compare a
YAML surface against the models rather than a model against itself. Both tiers read
this one block: the unit tier asserts it loads, the canonical tier asserts it spans
every constraint kind and every operator and survives the wire and the adapter
unchanged. A kind or an operator that stops being exercised here fails that span
assertion rather than quietly losing its coverage.
"""

from typing import Any

# Every operator, spread across the fields whose values each one reads. The
# ``status``/``result`` pairing is #717's rule: a result predicate is admitted only
# beside a status predicate reading exactly ``{equals: success}``.
EVERY_OPERATOR_MATCHER: dict[str, Any] = {
    "kind": "tool_call",
    "tool": {"equals": "billing_api_get_payment"},
    "executor": {"not_equals": "user"},
    "status": {"equals": "success"},
    "result": {"contains": "amount"},
    "args": {
        "payment_id": {"equals_ci": "pay-664306", "regex": "^PAY-[0-9]+$"},
        "amount": {"gt": 0.0, "gte": 1.0, "lt": 1000.0, "lte": 999.0},
        "currency": {"in_": ["USD", "EUR"], "not_in": ["JPY"]},
        "note": {"contains_ci": "REFUND", "len_gt": 3, "len_gte": 4},
        "body.resolution_path": {"exists": True},
    },
}

_DENIAL_MATCHER: dict[str, Any] = {
    "kind": "tool_call",
    "tool": {"equals": "servicenow_csm_update_case"},
    "args": {"u_resolution_code": {"equals": "denied_ineligible"}},
}

_POLICY_SEARCH_MATCHER: dict[str, Any] = {
    "kind": "tool_call",
    "tool": {"equals": "search_policy"},
}

_ASSISTANT_CONFIRMATION: dict[str, Any] = {
    "kind": "assistant_message",
    "text": {"contains": "confirm"},
}

_USER_APPROVAL: dict[str, Any] = {
    "kind": "user_message",
    "text": {"contains": "go ahead"},
}

_REFUND_RESULT: dict[str, Any] = {
    "kind": "tool_result",
    "tool": {"equals": "issue_refund"},
    "status": {"equals": "success"},
}

# One constraint per declared kind, keyed by the kind it exercises.
EVERY_CONSTRAINT_KIND: dict[str, dict[str, Any]] = {
    "present": {"present": {"match": EVERY_OPERATOR_MATCHER}},
    "absent": {"absent": {"match": {"kind": "tool_call", "tool": {"equals": "delete_account"}}}},
    "count": {"count": {"match": _POLICY_SEARCH_MATCHER, "min": 1, "max": 3}},
    "before": {
        "before": {
            "left": {"quantifier": "any", "match": _POLICY_SEARCH_MATCHER},
            "right": {"quantifier": "first", "match": _DENIAL_MATCHER},
        }
    },
    "immediately_before": {
        "immediately_before": {
            "left": {"quantifier": "last", "match": _ASSISTANT_CONFIRMATION},
            "right": {"quantifier": "all", "match": _USER_APPROVAL},
            "among": "messages",
        }
    },
    "absent_before": {
        "absent_before": {
            "forbidden": _DENIAL_MATCHER,
            "anchor": {"quantifier": "first", "match": _POLICY_SEARCH_MATCHER},
        }
    },
    "absent_between": {
        "absent_between": {
            "forbidden": _REFUND_RESULT,
            "start": {"quantifier": "first", "match": _POLICY_SEARCH_MATCHER},
            "end": {"quantifier": "last", "match": _DENIAL_MATCHER},
        }
    },
    "all_of": {
        "all_of": [
            {"present": {"match": _POLICY_SEARCH_MATCHER}},
            {"absent": {"match": _REFUND_RESULT}},
        ]
    },
    "any_of": {
        "any_of": [
            {"present": {"match": _ASSISTANT_CONFIRMATION}},
            {"present": {"match": _USER_APPROVAL}},
        ]
    },
    "negate": {"negate": {"present": {"match": _DENIAL_MATCHER}}},
}


# ``on_missing`` is rejected on the two kinds whose verdict is about matching
# nothing, so the block carries it on one kind that does anchor.
_ON_MISSING_KIND = "before"


def every_kind_block() -> dict[str, Any]:
    """The whole vocabulary as one authored ``trace_checks`` block."""
    constraints: list[dict[str, Any]] = []
    for kind, require in EVERY_CONSTRAINT_KIND.items():
        constraint: dict[str, Any] = {
            "id": f"exercises_{kind}",
            "description": f"the trajectory satisfies the {kind} constraint",
            "weight": 2.0,
            "within": {"first_turn": 0, "last_turn": 20},
            "require": require,
        }
        if kind == _ON_MISSING_KIND:
            constraint["on_missing"] = "pass"
        constraints.append(constraint)
    return {"constraints": constraints}
