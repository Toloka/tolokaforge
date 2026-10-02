"""Canonical test — the probe and the engine read reasoning the same way.

``scripts/analysis/probe_reasoning_transport.py`` is how a preset's reasoning
claim gets decided, and ``tests/canonical/test_reasoning_codec_preset_routing``
sends readers to it by name. A probe that enumerated channels differently from
the engine would hand out verdicts about a transport nobody runs — the audit
would pass while the engine lost reasoning, or the reverse.

So they share one definition, and this pins that they still do: the probe must
use :mod:`tolokaforge.core.llm.reasoning_transport`, and its "arrives readable,
not extracted" verdict must be exactly the condition under which the engine's
fallback recovers the reasoning.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.llm.reasoning_transport import (
    PermissiveReasoningReader,
    arriving_reasoning,
)

pytestmark = pytest.mark.canonical

_PROBE = Path(__file__).resolve().parents[2] / "scripts/analysis/probe_reasoning_transport.py"


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("_probe_reasoning_transport", _PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because @dataclass resolves annotations
    # through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class _Message:
    def __init__(self, reasoning_content=None, provider_specific_fields=None):
        self.reasoning_content = reasoning_content
        self.provider_specific_fields = provider_specific_fields


#: One recorded shape per answer the taxonomy can give. The o4-mini and o3 rows
#: are the shapes actually returned on 2026-10-02.
_RECORDED: dict[str, _Message] = {
    "o4-mini, readable sample": _Message(
        reasoning_content="I should read the log first.",
        provider_specific_fields={
            "reasoning": "I should read the log first.",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "I should read the log first."}
            ],
        },
    ),
    "o3, opaque sample": _Message(
        provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "xxxx"}]
        }
    ),
    "summary-style route": _Message(
        provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.summary", "summary": "checked the log"}]
        }
    ),
    "no reasoning at all": _Message(),
}


def test_the_probe_reads_through_the_engines_definition() -> None:
    probe = _load_probe()
    assert probe.arriving_reasoning is arriving_reasoning, (
        "The probe must import arriving_reasoning from "
        "tolokaforge.core.llm.reasoning_transport rather than keep its own copy. "
        "Two enumerations drift, and the probe is what decides preset claims."
    )


@pytest.mark.parametrize("label", sorted(_RECORDED))
def test_a_fix_verdict_is_exactly_what_the_fallback_recovers(label: str) -> None:
    """The audit's "FIX" and the engine's recovery name the same calls."""
    probe = _load_probe()
    message = _RECORDED[label]

    result = probe.ProbeResult(slug=label)
    result.arrived = arriving_reasoning(message)
    result.extracted = False  # as if the preset's codec kept nothing

    recovered = PermissiveReasoningReader().extract(message)
    says_fix = result.verdict == "FIX: arrives readable, not extracted"

    assert says_fix is (recovered is not None), (
        f"{label}: probe verdict {result.verdict!r} disagrees with the engine, "
        f"which recovered {'reasoning' if recovered else 'nothing'}."
    )
