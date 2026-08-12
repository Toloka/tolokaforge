"""Capability test — :attr:`Capability.TOOL_NAME_DISCIPLINE`.

Asserts that a model offered tools whose canonical names already
contain repeated underscore-separated segments echoes those names
verbatim — does NOT substitute ``:``, ``/``, or ``.`` for the
duplicated ``_``.

Concrete regression captured here — Gemini 3.1 Pro is observed to
substitute ``:`` for the duplicated ``_`` and emit names like
``workday_api:workday_api_get_employee`` that the harness rejects as
unknown. Every other registered model passes this test cleanly.

The two fake tools mirror a real-world doubled-prefix shape:

* ``workday_api_workday_api_get_employee`` (server prefix doubled).
* ``workday_api_workday_api_get_time_off`` (sibling sharing the same
  doubled prefix — forces the model to actively pick the right one,
  preventing a "collapse to anything that starts the same" shortcut).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

# Forbidden separators — anything in this tuple appearing inside an
# emitted tool name signals namespace-shape invention rather than
# verbatim echo of the registered name. See module docstring for the
# regression that motivated the list.
_FORBIDDEN_SEPARATORS: tuple[str, ...] = (":", "/", ".")

_REGISTERED_NAME = "workday_api_workday_api_get_employee"
_SIBLING_NAME = "workday_api_workday_api_get_time_off"


def _employee_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _REGISTERED_NAME,
            "description": "Look up an employee record by employee_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "Employee identifier, e.g. EMP-00078901.",
                    },
                },
                "required": ["employee_id"],
            },
        },
    }


def _time_off_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _SIBLING_NAME,
            "description": "List approved time-off entries for an employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                },
                "required": ["employee_id"],
            },
        },
    }


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_tool_name_discipline(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """The model's emitted tool name MUST equal the registered name verbatim.

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. The selected tool is ``workday_api_workday_api_get_employee``
       (the doubled-prefix domain tool, not the sibling).
    3. The emitted name carries no ``:`` / ``/`` / ``.`` separators —
       these are the substitution patterns observed in the eval
       regression and only appear when the model is reshaping the name
       rather than echoing it.
    """
    skip_unless_capability_declared(cert, Capability.TOOL_NAME_DISCIPLINE)

    client = live_client(cert)
    result = client.generate(
        system=(
            "You are an HR helper. When the user asks you to look something up, "
            "call the appropriate tool by its EXACT registered name. The names "
            "may contain repeated segments (this is intentional, not a typo). "
            "Do not insert any character that does not appear in the registered "
            "name."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content="Look up the employee record for EMP-00078901.",
            )
        ],
        tools=[_employee_tool(), _time_off_tool()],
        tool_choice="auto",
    )

    assert result.tool_calls, f"{cert.model_id}: no tool call emitted. text={result.text[:120]!r}"
    tc = result.tool_calls[0]

    assert tc.name == _REGISTERED_NAME, (
        f"{cert.model_id}: emitted invalid tool name {tc.name!r}. "
        f"Expected the verbatim registered name {_REGISTERED_NAME!r}. "
        "Common malformations: colon-namespace "
        "(workday_api:workday_api_get_employee), single-prefix "
        "(workday_api_get_employee), dotted "
        "(workday_api.workday_api_get_employee), or wrong sibling."
    )

    for sep in _FORBIDDEN_SEPARATORS:
        assert sep not in tc.name, (
            f"{cert.model_id}: emitted tool name {tc.name!r} contains forbidden "
            f"separator {sep!r}. The model is inventing a namespace shape rather "
            "than echoing the registered name verbatim."
        )
