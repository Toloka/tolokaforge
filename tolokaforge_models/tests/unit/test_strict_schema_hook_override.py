""":class:`StrictSchema` public hook — override contract.

Locks the public overridable hook :meth:`StrictSchema.inline_refs_in_tool`
against silent regression. A per-model subclass must be able to override the
hook at its public name and have the base's :meth:`~StrictSchema.sanitize`
pipeline route through the override rather than the base implementation.

Two properties matter here and both are asserted:

1. The base class exposes the hook at its public (non-underscored) name — a
   subclass' ``def inline_refs_in_tool(cls, tool)`` is a genuine override, not
   the accidental addition of a new attribute alongside a private
   ``_inline_refs_in_tool``.
2. :meth:`GeminiRecursiveSchema.inline_refs_in_tool` — the shipped override —
   remains distinct from the base implementation, so the cycle-tolerant path
   still fires on Gemini routes.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from tolokaforge_models.policies.gemini import GeminiRecursiveSchema

from tolokaforge.core.llm.schema_sanitizer import StrictSchema

pytestmark = pytest.mark.unit


_SIMPLE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": "no-op",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


class _MarkingStrict(StrictSchema):
    """Synthetic subclass — asserts the hook is genuinely overridable."""

    inline_refs_calls: ClassVar[list[Any]] = []

    @classmethod
    def inline_refs_in_tool(cls, tool: Any) -> Any:
        cls.inline_refs_calls.append(tool)
        marked = super().inline_refs_in_tool(tool)
        if isinstance(marked, dict):
            marked = dict(marked)
            marked["_overridden"] = True
        return marked


class TestStrictSchemaInlineRefsInToolHookOverride:
    def test_subclass_override_runs_on_sanitize(self) -> None:
        _MarkingStrict.inline_refs_calls = []
        result = _MarkingStrict().sanitize([_SIMPLE_TOOL])

        assert len(_MarkingStrict.inline_refs_calls) == 1, (
            "subclass override of `inline_refs_in_tool` did not run — the sanitize() "
            "pipeline is still routing through the base implementation. "
            "Check that `sanitize()` calls `self.inline_refs_in_tool(...)`, not the "
            "underscored private name."
        )
        assert result[0].get("_overridden") is True

    def test_hook_defined_as_classmethod_at_public_name(self) -> None:
        assert "inline_refs_in_tool" in StrictSchema.__dict__, (
            "`StrictSchema.inline_refs_in_tool` must be defined on the class body "
            "at its public name for subclasses to override it as a classmethod."
        )
        assert not hasattr(StrictSchema, "_inline_refs_in_tool"), (
            "`StrictSchema` must not define an underscored `_inline_refs_in_tool` "
            "attribute — the public name is the contract."
        )

    def test_gemini_recursive_override_distinct_from_base(self) -> None:
        assert GeminiRecursiveSchema.__dict__.get(
            "inline_refs_in_tool"
        ) is not StrictSchema.__dict__.get("inline_refs_in_tool"), (
            "`GeminiRecursiveSchema.inline_refs_in_tool` must remain a genuine override "
            "of the base — otherwise cyclic-$ref tolerance stops firing on Gemini routes."
        )
