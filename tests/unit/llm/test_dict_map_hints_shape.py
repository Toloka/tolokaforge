"""Lock the ``DictMapHints.build_hints`` public-hook shape.

``build_hints`` is an instance method (not a ``@staticmethod``) so subclasses
may override it while closing over ``self`` state. The name is public on both
:class:`DictMapHints` and :class:`RefResolvingDictMapHints` — no ``_build_hints``
attribute is defined on either class — and ``prompt_policy.py`` carries no
``type: ignore[override]`` pragma.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tolokaforge.core.llm import prompt_policy
from tolokaforge.core.llm.prompt_policy import DictMapHints, RefResolvingDictMapHints

pytestmark = pytest.mark.unit


def test_build_hints_is_instance_method_on_base() -> None:
    attr = inspect.getattr_static(DictMapHints, "build_hints")
    msg = f"DictMapHints.build_hints must be a plain instance method, not {type(attr).__name__}."
    assert inspect.isfunction(attr), msg
    assert not isinstance(attr, staticmethod)


def test_build_hints_first_parameter_is_self() -> None:
    params = list(inspect.signature(DictMapHints.build_hints).parameters)
    msg = f"DictMapHints.build_hints signature must lead with 'self'; got {params!r}"
    assert params[0] == "self", msg


def test_no_underscored_build_hints_attribute() -> None:
    base_msg = "DictMapHints must not define an underscored `_build_hints` attribute."
    assert not hasattr(DictMapHints, "_build_hints"), base_msg
    sub_msg = "RefResolvingDictMapHints must not define an underscored `_build_hints` attribute."
    assert not hasattr(RefResolvingDictMapHints, "_build_hints"), sub_msg


def test_subclass_override_is_named_build_hints() -> None:
    own = RefResolvingDictMapHints.__dict__.get("build_hints")
    assert own is not None, "RefResolvingDictMapHints must define its own build_hints override."
    parent = inspect.getattr_static(DictMapHints, "build_hints")
    assert own is not parent, "RefResolvingDictMapHints.build_hints must be a distinct override."


def test_prompt_policy_source_carries_no_type_ignore_override() -> None:
    source = Path(prompt_policy.__file__).read_text(encoding="utf-8")
    msg = "prompt_policy.py must not carry a 'type: ignore[override]' pragma."
    assert "type: ignore[override]" not in source, msg


def test_ref_resolving_dict_map_hints_build_hints_covers_nested_object() -> None:
    """Instance-method call resolves the nested-in-object dict-map the parent drops."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "submit_order",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order": {
                            "type": "object",
                            "properties": {
                                "lines": {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "object",
                                        "properties": {"qty": {"type": "integer"}},
                                        "required": ["qty"],
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    ]
    hints = RefResolvingDictMapHints().build_hints(tools)
    assert "submit_order" in hints
    assert "order.lines" in hints
    # Parent policy sees no top-level additionalProperties -> empty.
    assert DictMapHints().build_hints(tools) == ""
