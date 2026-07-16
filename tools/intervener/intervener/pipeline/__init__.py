"""LLM pipeline stages for :class:`LLMIntervener`.

M0/M2 scope: **situation classifier** + **message drafter**. Retrieval over
past interventions and calibrated urgency scoring land in M3 as part of the
DS413 lifecycle artifacts. The stages are wired so a later drop-in of
retrieval or a calibrated urgency head does not require rewriting the
participant.
"""

from intervener.pipeline.drafter import draft_suggestion

__all__ = ["draft_suggestion"]
