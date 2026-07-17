"""Coach package — a configurable participant that watches a running trial
and helps the agent when it gets stuck.

The coach is a `ComposedParticipant` from the intervener package, wired
from a `CoachConfig` (detector + intervener + cooldown + optional
budget). See `README.md` in the parent directory for the demo narrative.
"""

from coach.coach_participant import build_coach
from coach.config import CoachConfig, DetectorSpec, IntervenerSpec
from coach.cost_tracker import CoachReport, CostTrackingLLMCall

__all__ = [
    "CoachConfig",
    "CoachReport",
    "CostTrackingLLMCall",
    "DetectorSpec",
    "IntervenerSpec",
    "build_coach",
]
