"""The ``ToolWrapper`` base contract: text plus the substrate's failure verdict.

``execute`` answers with the tool's output text; ``execute_call`` answers with
that text *and* whether the substrate declared the call a failure. Only a
substrate with an out-of-band failure channel (MCP's ``isError``) overrides
``execute_call`` — every other wrapper signals a failed call by raising, and the
golden-replay loop records that raise. Both halves of that invariant are locked
here, because both are load-bearing and neither is visible from the type
signature alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import ToolCallOutcome, ToolWrapper

pytestmark = pytest.mark.unit


def _schema(name: str) -> ToolSchema:
    return ToolSchema(name=name, description=f"{name} for the contract test", parameters={})


class _AnsweringWrapper(ToolWrapper):
    """A wrapper over a substrate with no failure channel: it answers or raises."""

    def __init__(self, tool_schema: ToolSchema, answer: str | Exception):
        super().__init__(tool_schema)
        self._answer = answer

    async def execute(self, arguments: dict[str, Any]) -> str:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


async def test_inherited_execute_call_reports_the_text_as_a_non_failure() -> None:
    wrapper = _AnsweringWrapper(_schema("get_customer"), '{"id": "C-101"}')

    outcome = await wrapper.execute_call({"customer_id": "C-101"})

    assert outcome == ToolCallOutcome(output='{"id": "C-101"}', declared_failure=False)


async def test_inherited_execute_call_lets_a_raising_tool_raise() -> None:
    """A raise must travel out of ``execute_call`` untouched.

    Raising is how every wrapper without an out-of-band failure channel reports a
    failed call, and the golden-replay loop's ``except Exception`` is what records
    it. A ``try/except`` inside the default ``execute_call`` would convert every
    raised tau / MCP-async golden failure into ``declared_failure=False`` and
    record nothing, so this assertion — not the docker-gated replay suite — is
    what makes that regression visible on a pull request.
    """
    boom = RuntimeError("db-service refused the connection")
    wrapper = _AnsweringWrapper(_schema("place_order"), boom)

    with pytest.raises(RuntimeError, match="db-service refused the connection") as excinfo:
        await wrapper.execute_call({"customer_id": "C-101"})

    assert excinfo.value is boom
