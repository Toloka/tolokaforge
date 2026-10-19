""":class:`ScalarArrayDictMapResponse` under both engine contracts.

With ``param_schema`` (an engine that passes the tool's original schema) the
inherited schema-driven reverse pivot does everything — scalar unwrap and
nested reversal at any depth — and the class returns it untouched. Without it
(an older engine) the shape heuristics it shipped with still apply: root
scalar unwrap plus a one-level pivot into object params.
"""

from __future__ import annotations

from typing import Any

import pytest
from tolokaforge_models.policies.gemini import ScalarArrayDictMapResponse

pytestmark = pytest.mark.unit


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
        "order": {
            "type": "object",
            "properties": {
                "meta": {
                    "type": "object",
                    "properties": {
                        "tags": {"type": "object", "additionalProperties": {"type": "string"}}
                    },
                }
            },
        },
    },
}


def test_with_schema_reverses_two_levels_deep() -> None:
    emitted = {
        "counts": [{"key": "a", "value": 1}],
        "order": {"meta": {"tags": [{"key": "env", "value": "prod"}]}},
    }
    out = ScalarArrayDictMapResponse().parse_arguments(
        emitted, param_types={"counts": "dict_map", "order": "object"}, param_schema=_SCHEMA
    )
    assert out == {"counts": {"a": 1}, "order": {"meta": {"tags": {"env": "prod"}}}}


def test_without_schema_keeps_the_shipped_heuristics() -> None:
    emitted = {
        "counts": [{"key": "a", "value": 1}],
        "order": {"meta": {"tags": [{"key": "env", "value": "prod"}]}},
    }
    out = ScalarArrayDictMapResponse().parse_arguments(
        emitted, param_types={"counts": "dict_map", "order": "object"}
    )
    # Root scalar unwrap fires; the two-levels-deep map is beyond the
    # one-level reach the heuristic path documents.
    assert out["counts"] == {"a": 1}
    assert out["order"]["meta"]["tags"] == [{"key": "env", "value": "prod"}]
