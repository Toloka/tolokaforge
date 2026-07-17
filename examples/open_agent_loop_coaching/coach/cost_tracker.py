"""Cost tracking for the coach's LLM calls + the per-trial coach report.

The coach's LLM calls don't appear in tolokaforge's `Usage` accounting
because they go through the intervener's `LLMCallable` seam (not the
`LLMClient`'s per-call usage tracker). To keep the A/B numbers honest
we wrap the coach's `LLMCallable` here and record each call's cost —
inferred from token counts + a fixed price table, or supplied
externally by the caller.

The default cost table is deliberately conservative — real prices differ
per provider and per contract. Callers that need exact accounting should
subclass or pass a `cost_per_1k_input` / `cost_per_1k_output` override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from intervener.tools.base import LLMCallable

__all__ = ["CoachReport", "CostTrackingLLMCall"]


# Rough USD-per-1k-token defaults for the OpenRouter-fronted Claude
# family used in the demo configs. Under-estimates for large models; a
# demo-grade approximation. Override via constructor kwargs for exact
# billing.
_DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # (input_per_1k, output_per_1k)
    "claude-haiku-4.5": (0.001, 0.005),
    "claude-sonnet-4.6": (0.003, 0.015),
    "default": (0.003, 0.015),
}


@dataclass
class CoachReport:
    """Per-trial coach bookkeeping — one instance per (participant, trial).

    Written to `results/<arm>/trials/<task_id>/<idx>/coach_report.yaml`
    at trial end. Consumed by `analyze_results.py` for the A/B summary.
    """

    trial_id: str
    coach_id: str
    detector_type: str
    intervener_type: str

    interventions_submitted: int = 0
    interventions_by_kind: dict[str, int] = field(default_factory=dict)
    ack_outcomes: dict[str, int] = field(default_factory=dict)
    trigger_events: list[dict[str, Any]] = field(default_factory=list)
    coach_llm_calls: int = 0
    coach_input_tokens: int = 0
    coach_output_tokens: int = 0
    coach_cost_usd: float = 0.0
    llm_errors: int = 0

    def record_trigger(self, detector: str, reason: str, at_seq: int) -> None:
        self.trigger_events.append(
            {
                "at_seq": at_seq,
                "detector": detector,
                "reason": reason,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    def record_submission(self, kind: str, ack_outcome: str) -> None:
        self.interventions_submitted += 1
        self.interventions_by_kind[kind] = self.interventions_by_kind.get(kind, 0) + 1
        self.ack_outcomes[ack_outcome] = self.ack_outcomes.get(ack_outcome, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "coach_id": self.coach_id,
            "detector_type": self.detector_type,
            "intervener_type": self.intervener_type,
            "interventions_submitted": self.interventions_submitted,
            "interventions_by_kind": dict(self.interventions_by_kind),
            "ack_outcomes": dict(self.ack_outcomes),
            "trigger_events": list(self.trigger_events),
            "coach_llm_calls": self.coach_llm_calls,
            "coach_input_tokens": self.coach_input_tokens,
            "coach_output_tokens": self.coach_output_tokens,
            "coach_cost_usd": round(self.coach_cost_usd, 6),
            "llm_errors": self.llm_errors,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False)


class CostTrackingLLMCall:
    """Wraps an :data:`LLMCallable` and bills each call into a
    :class:`CoachReport`.

    The token counts are estimated via a coarse whitespace-based
    tokeniser when the underlying callable returns only text. Callers
    that have real token counts (e.g. by intercepting the LLMClient's
    Usage payload) can override ``compute_tokens``.
    """

    def __init__(
        self,
        inner: LLMCallable,
        report: CoachReport,
        model_key: str = "default",
        price_table: dict[str, tuple[float, float]] | None = None,
        budget_usd: float | None = None,
    ) -> None:
        self._inner = inner
        self._report = report
        self._model_key = model_key
        self._prices = price_table or _DEFAULT_PRICE_TABLE
        self._budget = budget_usd

    def __call__(self, system: str, user: str) -> str:
        # Budget gate: refuse to spend past the cap. Returning empty makes
        # the detector/intervener fall back to its no-LLM path.
        if self._budget is not None and self._report.coach_cost_usd >= self._budget:
            return ""

        try:
            text = self._inner(system, user)
        except Exception:
            self._report.llm_errors += 1
            raise

        in_tokens = _estimate_tokens(system) + _estimate_tokens(user)
        out_tokens = _estimate_tokens(text)
        in_price, out_price = self._prices.get(
            self._model_key, self._prices.get("default", (0.003, 0.015))
        )
        cost = (in_tokens / 1000) * in_price + (out_tokens / 1000) * out_price

        self._report.coach_llm_calls += 1
        self._report.coach_input_tokens += in_tokens
        self._report.coach_output_tokens += out_tokens
        self._report.coach_cost_usd += cost
        return text


def _estimate_tokens(text: str) -> int:
    """Rough whitespace-based tokeniser. ~1.3× word count as a proxy for
    real BPE tokens — good enough for a demo. Real accounting should
    plumb the provider's actual token counts through."""
    if not text:
        return 0
    words = len(text.split())
    return max(1, int(words * 1.3))
