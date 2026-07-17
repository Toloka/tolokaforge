"""Pydantic config for a coach — YAML-serialisable, `extra="forbid"`.

The coach is composed at runtime from two pluggable specs:

* :class:`DetectorSpec` — chooses the strategy that decides *when* to help
  (rule-based, LLM-analysis, always, never).
* :class:`IntervenerSpec` — chooses *what* the coach does when it fires
  (canned hint, LLM-drafted suggestion, kill after N stucks).

Both specs use the `type: str + params: dict` shape the harness already
uses for adapters — a factory in `detectors.py` / `interveners.py` maps
`type` to a concrete class. Keeps the YAML file the single source of
truth for coach behaviour; no code change needed to try a new
combination.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CoachConfig", "DetectorSpec", "IntervenerSpec"]


class DetectorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    """One of: 'rule', 'llm', 'always', 'never'. See `coach.detectors`."""

    params: dict[str, Any] = Field(default_factory=dict)
    """Detector-specific parameters. See each detector's docstring."""


class IntervenerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    """One of: 'hint', 'llm_suggest', 'kill'. See `coach.interveners`."""

    params: dict[str, Any] = Field(default_factory=dict)


class CoachConfig(BaseModel):
    """Complete coach spec — one detector + one intervener + policy knobs."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str = "coach"
    role: str = "participant"
    """One of: 'observer', 'participant', 'admin'. Admin can kill."""

    detector: DetectorSpec
    intervener: IntervenerSpec

    cooldown_turns: int = 1
    """After firing, wait this many events before firing again. Prevents
    the coach from spamming after a single stuck detection."""

    budget_usd: float | None = None
    """Optional per-trial LLM budget cap for the coach. When set, the
    driver stops passing the wrapped LLMCallable to the coach once
    cumulative coach cost exceeds this. `None` = no cap."""
