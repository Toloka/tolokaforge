"""What a generation cost on a trace: the charge the provider stated, else the eval's figure.

A trial bundle records two cost figures per LLM call (``metrics.yaml`` ``usage.calls[*]``):
``cost_usd``, the eval's own figure (litellm's, or the engine's pricing table, as ``cost_source``
says), and ``billed_cost_usd``, what the response stated the call was charged, absent on a route
that states none and in a bundle written before the field existed. ``grade.judge_usage`` carries
both for the judge, summed over its calls.

A generation's Langfuse cost (``costDetails.total``) is the billed amount where there is one and
the eval's figure where there is not, and ``cost_basis`` in its metadata names which, so a reader
tells an actual charge from an estimate. Langfuse adds a trace's generation costs into the trace's
cost itself, so nothing here totals a trial.

Engine-free: it reads bundle mappings and call records, never an engine type, so the offline
connector can apply the same rules (it keeps its own copy, held to these by the shared golden).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NONE = "none"
COST_BASIS_BILLED = "billed"  # the provider stated the charge
COST_BASIS_LITELLM = "litellm"  # the eval's figure, from litellm (``cost_source: litellm``)
COST_BASIS_LIST = "list"  # the eval's figure, from the engine's pricing table (``local``)
COST_BASIS_EVAL = "eval"  # the eval's figure, its source not recorded (the judge's aggregate)
_BASIS_BY_SOURCE = {"litellm": COST_BASIS_LITELLM, "local": COST_BASIS_LIST}


def amount(value: object) -> float | None:
    """A number a bundle states, as a float; anything else (absent, a bool, text) is ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _priced(billed: float | None, eval_cost: float | None, basis: str) -> tuple[float | None, str]:
    if billed is not None:
        return billed, COST_BASIS_BILLED
    if eval_cost is not None:
        return eval_cost, basis
    return None, NONE


def call_cost(call: Mapping[str, Any]) -> tuple[float | None, str]:
    """(the generation's ``costDetails.total`` or ``None``, its ``cost_basis``) for one call.

    The basis is ``billed``, else the eval's source (``litellm``, ``list``, or ``eval`` for a
    figure whose source the record does not name), else ``none`` without any cost."""
    return _priced(
        amount(call.get("billed_cost_usd")),
        amount(call.get("cost_usd")),
        _BASIS_BY_SOURCE.get(str(call.get("cost_source")), COST_BASIS_EVAL),
    )


def judge_cost(judge_usage: Mapping[str, Any]) -> tuple[float | None, str]:
    """(``costDetails.total`` or ``None``, ``cost_basis``) of the judge generation that carries
    ``grade.judge_usage``, the aggregate over the judge's calls (the bundle records none per call):
    the billed sum when every judge call stated a charge, else the eval's figure."""
    return _priced(
        amount(judge_usage.get("billed_cost_usd")),
        amount(judge_usage.get("cost_usd")),
        COST_BASIS_EVAL,
    )


def call_record(call: object) -> dict[str, Any]:
    """The cost fields of an in-memory call record (the engine's ``ProviderRawCall``), as the
    mapping the bundle would hold: the live observer prices a generation before any bundle."""
    return {
        name: getattr(call, name, None) for name in ("cost_usd", "cost_source", "billed_cost_usd")
    }
