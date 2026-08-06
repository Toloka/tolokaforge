"""Live regression test: the user simulator must not restart the conversation.

Reproduces the CBT-021 failure shape against a real user-model. In that run
the simulator's first live turn fired after the agent had fully answered the
seeded opening, but the simulator's flipped context had its own opening
trimmed away — so the model believed it had not asked yet and re-sent the
scripted opening verbatim, restarting the conversation (duplicate tickets,
``state_checks`` fail). The context-construction fix is locked deterministically
by ``tests/unit/test_user_simulator_context.py``; this test pins the
behavioural consequence with the same user model the run used.

Run with:
    scripts/with_env.sh uv run pytest tests/integration/test_user_simulator_live.py \\
        -q -m integration
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from tolokaforge.core.llm.client import UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.llm,
]

_TS = datetime(2026, 8, 4, 13, 26, 0, tzinfo=timezone.utc)

_OPENING = (
    "Hi, this is Mortimer Pemberton, account 13000019. I just wanted to check "
    "whether a corrected 1099-B has been issued for my account for tax year 2024?"
)

# Condensed from the CBT-021 task pack backstory — keeps the two ingredients
# that made the bug bite: the exact quoted opening line and the instruction to
# open with it.
_BACKSTORY = f"""You are a customer contacting customer service.

Your details:
You are Mortimer Pemberton contacting support to ask whether a corrected 1099-B
has been issued on your account (13000019) for tax year 2024.

Your reason for contacting support:
"{_OPENING}"

Start the conversation by expressing this need exactly once. After the agent
provides the final outcomes, acknowledge them briefly and end the conversation;
do not repeat or restart the original request."""

_AGENT_ANSWER = (
    "Hi Mortimer. I checked your 2024 tax documents. No corrected Form 1099-B "
    "has been issued; only the original 1099-B is currently on file."
)


@pytest.fixture()
def simulator() -> UserSimulator:
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — skipping live simulator test")
    return UserSimulator(
        mode="llm",
        llm_config=ModelConfig(
            provider="openrouter",
            name="anthropic/claude-sonnet-4.6",
            temperature=0.0,
            max_tokens=1024,
        ),
        backstory=_BACKSTORY,
    )


def test_simulator_does_not_restart_after_agent_answers(simulator: UserSimulator) -> None:
    """First live simulator turn after a full answer must continue, not reopen.

    The shared transcript is exactly the CBT-021 shape at the failure point:
    seeded opening, agent tool-call turns (no dialogue text), tool results,
    then the agent's complete answer.
    """
    context = [
        Message(role=MessageRole.USER, content=_OPENING, ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),
        Message(
            role=MessageRole.TOOL,
            content=(
                '{"documents": [{"id": "TAX-00077006", "account_id": "13000019", '
                '"tax_year": 2024, "document_type": "1099_b", "status": "original", '
                '"corrected_flag": false}]}'
            ),
            ts=_TS,
        ),
        Message(role=MessageRole.ASSISTANT, content=_AGENT_ANSWER, ts=_TS),
    ]

    result = simulator.reply(context)
    reply = result.text.strip()

    assert reply, "simulator returned an empty reply"
    # The bug's signature was the scripted opening re-sent verbatim. Any
    # substantial overlap with the opening request means the simulator is
    # reopening a resolved conversation.
    assert (
        "wanted to check whether a corrected 1099-B" not in reply
    ), f"simulator restarted the conversation: {reply!r}"
    assert not reply.startswith(
        "Hi, this is Mortimer Pemberton"
    ), f"simulator restarted the conversation: {reply!r}"
