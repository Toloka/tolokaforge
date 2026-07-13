# Copyright 2025 The Harbor Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Failure classification adapted from Harbor's installed-agent base contract."""

from __future__ import annotations

from dataclasses import dataclass

from tolokaforge.core.models import TerminationReason, TrialStatus


@dataclass(frozen=True)
class ClassifiedExit:
    status: TrialStatus
    reason: TerminationReason


def classify_agent_exit(*, exit_code: int, output: str, timed_out: bool) -> ClassifiedExit:
    """Map provider and transport failures onto Tolokaforge retry semantics."""

    text = output.lower()
    if timed_out:
        return ClassifiedExit(TrialStatus.TIMEOUT, TerminationReason.TIMEOUT)
    if exit_code == 0:
        return ClassifiedExit(TrialStatus.COMPLETED, TerminationReason.AGENT_DONE)
    if any(marker in text for marker in ("usage limit", "credit balance", "quota exhausted")):
        return ClassifiedExit(TrialStatus.FAILED, TerminationReason.USAGE_EXHAUSTED)
    if any(marker in text for marker in ("safety refusal", "policy refusal")):
        return ClassifiedExit(TrialStatus.FAILED, TerminationReason.SAFETY_REFUSAL)
    if any(marker in text for marker in ("rate limit", "overloaded", "too many requests")):
        return ClassifiedExit(TrialStatus.ERROR, TerminationReason.RATE_LIMIT)
    if any(
        marker in text
        for marker in (
            "connection refused",
            "connection closed",
            "connection reset",
            "timed out",
            "temporary failure in name resolution",
        )
    ):
        return ClassifiedExit(TrialStatus.ERROR, TerminationReason.API_TIMEOUT)
    return ClassifiedExit(TrialStatus.ERROR, TerminationReason.API_ERROR)
