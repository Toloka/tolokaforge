"""Canonical test — which presets are allowed to keep no reasoning.

A codec whose ``encode_for_replay`` returns nothing means the model never sees
its own reasoning again. That is **correct** for OpenAI, whose routes refuse
echoed reasoning, and a silent defect everywhere else: the model reads a
history in which it never reasoned and stops reasoning. Measured on
``moonshotai/kimi-k2.7-code``, which went from reasoning on turn 1 and nothing
afterwards to reasoning on 98% of turns once the codec replayed, with score
0.380 → 0.688.

So the non-replaying codec is an allow-list, not a default. A preset joining
this list is claiming its route refuses echoed reasoning, which
``scripts/analysis/probe_reasoning_transport.py`` answers in one live call.

This pins the *routing*. The runtime counters on ``Metrics``
(``reasoning_billed_not_captured`` / ``reasoning_replay_dropped``) catch the
same defect for a model nobody thought to list here, and the capability
registry refuses an unjustified "this model has no usable reasoning" claim.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import _instantiate_slot, get_resolved_presets
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning

pytestmark = pytest.mark.canonical


#: One block of ordinary unsigned reasoning — what a summary-style route
#: returns. Asking the codec what it would send back for this is the whole
#: predicate: a class check would be wrong, because the codec that *does*
#: replay subclasses the one that does not.
_SAMPLE = StructuredReasoning(blocks=(ReasoningBlock(type="summary_text", text="I read the log."),))

#: Presets whose route genuinely refuses echoed reasoning, with the reason.
#: Adding an entry is a claim about the provider, not a way to quiet this test.
_MAY_KEEP_NOTHING: dict[str, str] = {
    "openai_gpt5": "OpenAI does not accept echoed reasoning on subsequent turns",
    "openai_gpt6": "OpenAI does not accept echoed reasoning on subsequent turns",
    "gemma": "no reasoning surface on this lineage",
    "deepseek_v32": "probed 2026-09-30 on SiliconFlow: no reasoning surfaced at all",
    "minimax": "probed 2026-09-30 on GMICloud: no reasoning surfaced at all",
    "cohere_command_a_plus": (
        "not probed — this preset targets the Azure AI spelling of the slug, which this "
        "account cannot reach; the claim is inherited and untested, which is itself the "
        "reason and should be revisited when the route becomes reachable"
    ),
    "aws_nova": "not probed — Bedrock route not reachable from this account",
    "aws_nova_openrouter": "not probed — the OpenRouter nova slug 404s from this account",
}


def _preset_names() -> list[str]:
    return sorted(get_resolved_presets()["presets"])


def test_only_the_allow_list_ships_a_codec_that_replays_nothing() -> None:
    offenders: dict[str, str] = {}
    for name, block in get_resolved_presets()["presets"].items():
        if name in _MAY_KEEP_NOTHING:
            continue
        # Read the codec off the preset block rather than resolving a model
        # through it: a vendor-wildcard glob has no slug to de-glob, and
        # guessing one silently tested the fallback preset instead.
        codec = _instantiate_slot(block, "reasoning_codec", f"presets.{name}")
        try:
            replay = codec.encode_for_replay(_SAMPLE)
        except ValueError:
            # The codec refused this block shape, which is the Protocol's rule
            # for malformed input — it reads the field and has an opinion, so
            # it is not dropping reasoning on the floor.
            continue
        if not replay:
            offenders[name] = type(codec).__name__

    assert not offenders, (
        f"These presets resolve to a codec that replays no reasoning: {offenders}.\n\n"
        "That is how kimi-k2.7-code lost half its score on Terminal-Bench. Run "
        "scripts/analysis/probe_reasoning_transport.py against the model: if readable "
        "reasoning arrives, route the preset to `openai_summary_replay`; if the route "
        "returns only an opaque blob or refuses the echo, add it to _MAY_KEEP_NOTHING "
        "with that as the reason."
    )


def test_every_allow_list_entry_still_names_a_preset() -> None:
    """A stale exemption hides a preset that has since changed underneath it."""
    known = set(_preset_names())
    stale = sorted(name for name in _MAY_KEEP_NOTHING if name not in known)

    assert not stale, (
        f"_MAY_KEEP_NOTHING names presets that no longer exist: {stale}. "
        "Remove them, so the list keeps meaning what it says."
    )
