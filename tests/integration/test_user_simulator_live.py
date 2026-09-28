"""Live regression test: the user simulator must not restart the conversation.

Exercises the failure shape behind the seeded-opening fix against a real
user-model. The reproducing recipe is generic: a backstory that quotes the
exact opening line and instructs the simulator to open with it exactly once,
plus a shared transcript in which the agent has already fully answered that
opening. When the simulator's own opening is missing from its flipped view,
the model re-sends the scripted opening, restarting the conversation
(pre-fix reproduction evidence — a character-for-character re-send — is in
PR #905). What this test pins: the reply neither re-sends the opening nor
re-introduces the customer as if unanswered. A paraphrased re-ask that skips
the self-introduction is out of its reach — that judgement needs a grader,
not a substring. The context-construction lock itself is deterministic in
``tests/unit/test_user_simulator_context.py``; see tests/README.md
§ "Live user-simulator no-restart regression" for the cost note and why the
deterministic lock alone is not considered sufficient.

Run with:
    scripts/with_env.sh uv run pytest tests/integration/test_user_simulator_live.py \\
        -q -m integration
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.client import UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.llm,
]

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_OPENING = (
    "Hi, this is Alex Quill, customer number 55001234. I just wanted to check "
    "whether a replacement warranty certificate has been issued for my order for "
    "purchase year 2025?"
)

# Synthetic backstory keeping the two ingredients that made the bug bite: the
# exact quoted opening line and the instruction to open with it exactly once.
_BACKSTORY = f"""You are a customer contacting customer service.

Your details:
You are Alex Quill contacting support to ask whether a replacement warranty
certificate has been issued on your customer account (55001234) for purchase
year 2025.

Your reason for contacting support:
"{_OPENING}"

Start the conversation by expressing this need exactly once. After the agent
provides the final outcomes, acknowledge them briefly and end the conversation;
do not repeat or restart the original request."""

_AGENT_ANSWER = (
    "Hi Alex. I checked your 2025 purchase records. No replacement warranty "
    "certificate has been issued; only the original certificate is currently on file."
)


@pytest.fixture()
def simulator() -> UserSimulator:
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — skipping live simulator test")
    # ``_llm_reply`` pins its own generation temperature; the config value is
    # not the control here, so none is claimed.
    return UserSimulator(
        mode="llm",
        llm_config=ModelConfig(
            provider="openrouter",
            name="anthropic/claude-sonnet-4.6",
            max_tokens=1024,
        ),
        backstory=_BACKSTORY,
    )


def test_simulator_does_not_restart_after_agent_answers(simulator: UserSimulator) -> None:
    """First live simulator turn after a full answer must continue, not reopen.

    The shared transcript is exactly the failure shape: seeded opening, an
    agent tool-call turn (no dialogue text), a tool result, then the agent's
    complete answer.
    """
    context = [
        Message(role=MessageRole.USER, content=_OPENING, ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),
        Message(
            role=MessageRole.TOOL,
            content=(
                '{"certificates": [{"id": "CERT-0042", "customer_id": "55001234", '
                '"purchase_year": 2025, "status": "original", "replaced_flag": false}]}'
            ),
            ts=_TS,
        ),
        Message(role=MessageRole.ASSISTANT, content=_AGENT_ANSWER, ts=_TS),
    ]

    result = simulator.reply(context)
    reply = result.text.strip()

    assert reply, "simulator returned an empty reply"
    # The bug's signature: the scripted opening re-sent as if unanswered.
    # A restart — verbatim or lightly paraphrased — re-introduces the caller
    # (name, customer number) or re-states the request line; an
    # acknowledgement or a short confirming follow-up does neither.
    restarted = (
        "wanted to check whether" in reply
        or reply.startswith("Hi, this is Alex Quill")
        or "55001234" in reply
    )
    assert not restarted, f"simulator restarted the conversation: {reply!r}"


def _terminating_agent() -> MagicMock:
    """A mock agent that ends the trial after one free turn.

    Empty ``usage.calls`` and ``cost_usd=None`` mean the agent contributes no
    spend, so the trial's cost is exactly the real user simulator's spend.
    """
    agent = MagicMock()
    agent.model_name = "mock-agent"
    agent.capabilities.max_context_tokens = None
    agent.capabilities.context_watermark = None
    agent.generate.return_value = GenerationResult(
        text="Thanks, that's all I needed.",
        tool_calls=[],
        usage=Usage(),
        cost_usd=None,
    )
    return agent


def test_simulator_spend_lands_in_trial_metrics(simulator: UserSimulator) -> None:
    """A live conversational trial folds the user simulator's real spend into
    the trial ``Metrics``, while the USER ``Message`` keeps no generation id.

    Only the user side hits the wire (the agent is a free terminating mock), so
    the trial's ``cost_usd`` is exactly the simulator's spend and the ``role``
    stamped on ``usage.calls`` is provably the user's.
    """
    executor = MagicMock()
    executor.execute.return_value = ToolResult(success=True, output="ok")
    runner = TrialRunner(
        task_id="live-user-spend",
        trial_index=0,
        agent_client=_terminating_agent(),
        user_simulator=simulator,
        tool_executor=executor,
        tool_schemas=[{"type": "function", "function": {"name": "noop"}}],
        max_turns=1,
        turn_timeout_s=60,
        episode_timeout_s=600,
    )
    # Empty initial message => turn 0 is bootstrapped by the real simulator,
    # which is the only wire call in the trial.
    trajectory = runner.run("You are a helpful support agent.", "")

    metrics = trajectory.metrics
    user_rows = [c for c in metrics.usage.calls if c.role == "user"]
    assert user_rows, "no user-role call was recorded in the trial metrics"
    assert metrics.cost_usd is not None and metrics.cost_usd > 0.0
    gen_ids = [c.openrouter_generation_id for c in user_rows if c.openrouter_generation_id]
    assert gen_ids, "the user call carried no OpenRouter generation id"
    assert set(gen_ids) <= set(metrics.openrouter_generation_ids)

    user_messages = [m for m in trajectory.messages if m.role == MessageRole.USER]
    assert user_messages
    assert all(m.openrouter_generation_id is None for m in user_messages)
