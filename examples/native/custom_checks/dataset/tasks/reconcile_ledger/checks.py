"""Arithmetic custom checks for the ledger-reconciliation reference task.

The two checks together exercise the deterministic-Python gap the
`custom_checks` seam exists for: arithmetic scoring on the final DB state
combined with transcript evidence that no declarative state / transcript
primitive expresses.

    1. `balance_matches_transaction_net` — sums the `transactions` table
       (credits positive, debits negative), adds the recorded opening
       balance, and asserts `customers[id='C-1'].balance` equals the
       computed total.
    2. `transcript_enumerates_credit_transactions` — verifies the agent
       actually inspected the ledger by looking for every credit
       transaction id in the transcript's tool-call arguments or message
       content, and that at least one `db_query` was issued.
"""

from __future__ import annotations

from tolokaforge.core.grading.checks_helpers import find_by_key, tool_was_called
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    check,
    init,
)

_CREDIT_TXN_IDS = ("T-1", "T-3", "T-5")

_ctx: CheckContext | None = None


@init(interface_version="1.0")
def _load_context(ctx: CheckContext) -> None:
    global _ctx
    _ctx = ctx


def _final_state_data() -> dict:
    assert _ctx is not None, "checks accessed before @init ran"
    return _ctx.final_state.data


def _transcript_haystack() -> str:
    """Concatenate every content string + every tool-call argument value.

    The runner-side transcript builder decodes wire tool calls into
    `ToolCall(name, arguments)` (result is not carried on the wire), so the
    haystack must look at both message content and tool-call argument
    dicts to reflect what evidence a check actually has access to.
    """
    assert _ctx is not None, "checks accessed before @init ran"
    parts: list[str] = []
    for msg in _ctx.transcript.messages:
        parts.append(msg.content or "")
        for tc in msg.tool_calls:
            for value in (tc.arguments or {}).values():
                parts.append(str(value))
    return "\n".join(parts)


@check
def balance_matches_transaction_net() -> CheckPassed | CheckFailed:
    """`balance == opening_balance + sum(credits) - sum(debits)`."""
    state = _final_state_data()
    customers = state.get("customers", [])
    transactions = state.get("transactions", [])
    customer = find_by_key(customers, "id", "C-1")
    if customer is None:
        return CheckFailed("customer 'C-1' missing from final state")

    opening = customer.get("opening_balance")
    if not isinstance(opening, (int, float)):
        return CheckFailed(
            f"customer.opening_balance not numeric: {opening!r}",
            details={"opening_balance": opening},
        )

    credits = sum(t["amount"] for t in transactions if t.get("kind") == "credit")
    debits = sum(t["amount"] for t in transactions if t.get("kind") == "debit")
    expected = opening + credits - debits
    actual = customer.get("balance")
    if actual == expected:
        return CheckPassed(
            f"balance {actual} == opening {opening} + credits {credits} - debits {debits}"
        )
    return CheckFailed(
        f"balance {actual!r} != opening {opening} + credits {credits} - debits {debits} = {expected}",
        details={
            "actual": actual,
            "expected": expected,
            "opening_balance": opening,
            "credits": credits,
            "debits": debits,
        },
    )


@check
def transcript_enumerates_credit_transactions() -> CheckPassed | CheckFailed:
    """Every credit transaction id must appear in the transcript."""
    assert _ctx is not None, "checks accessed before @init ran"
    if not tool_was_called(_ctx.tool_calls, "db_query"):
        return CheckFailed("no db_query call in transcript — ledger not inspected")

    haystack = _transcript_haystack()
    missing = [tid for tid in _CREDIT_TXN_IDS if tid not in haystack]
    if missing:
        return CheckFailed(
            f"credit transactions not enumerated in transcript: {missing}",
            details={"missing": missing, "expected": list(_CREDIT_TXN_IDS)},
        )
    return CheckPassed(f"all credit transaction ids enumerated: {list(_CREDIT_TXN_IDS)}")
