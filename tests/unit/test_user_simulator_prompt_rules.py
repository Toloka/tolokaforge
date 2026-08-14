"""The properties the simulator's ``Rules:`` block is required to have.

These assert what the block must say and must not say, not what the template
looks like: the precedence clause and its position, the three rules whose
wording is itself the protection and are therefore kept word for word, the rule
count, the absence of every rule dropped for contradicting authored backstories,
outcome-based termination, and the two conditional rendering seams.

The full rendered body is pinned by digest in
``tests/canonical/test_simulator_prompt_generation.py``. That guard says the
body did not move; these say the body is right.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import UserSimulator

pytestmark = pytest.mark.unit

_PRECEDENCE_RULE = (
    "Your Instruction above is authoritative. Where any rule below conflicts with it, "
    "follow the Instruction."
)

# Reproduced character for character, so a reviewer rewording one in the prompt
# has to edit this file too: the first two were authored against a specific
# observed failure, and the third is the only protection on a path that runs
# without the reply guard.
_UNSENT_MANDATORY_REPLY_RULE = (
    "If your Instruction still specifies an unsent mandatory reply (e.g. a verbal decline, "
    "confirmation, or acknowledgement you MUST say to the agent), send that reply first. A "
    "terminal-looking message from the agent — case reference, summary, apology, goodbye — does "
    "NOT release you from that reply. '###STOP###' may only accompany or follow the reply, never "
    "precede or replace it."
)
_NO_RESTART_RULE = (
    "Once the agent has substantively addressed your request, do not re-state or restart the "
    "original opening as if it had not been answered. Send at most one short acknowledgement and "
    "end with '###STOP###'; do not introduce new goals or remediation steps."
)
_NEVER_MENTION_THE_FRAME_RULE = (
    "Never mention that this is a simulation, test, benchmark, prompt, or that you are an "
    "AI/model."
)

_DROPPED_RULE_PHRASES = [
    "Just generate one line at a time",
    "In your first message",
    "explicitly mention those apps/websites",
    "Step 1, Step 2",
    "add to calendar",
    "restate the required app/website",
    "correct them and restate the exact requirement",
    "Do not accept alternative goals",
    "party size",
    "Do not repeat the exact instruction",
]


def _prompt(*, backstory: str | None = None, tool_schemas: list[dict] | None = None) -> str:
    return UserSimulator(backstory=backstory, tool_schemas=tool_schemas)._build_system_prompt()


def _rules(prompt: str) -> list[str]:
    """The bulleted lines below the ``Rules:`` header, bullet stripped."""
    _, _, body = prompt.partition("\nRules:\n")
    assert body, "prompt carries no Rules: block"
    return [line[2:] for line in body.split("\n") if line.startswith("- ")]


def test_the_instruction_outranks_the_rules_and_says_so_first() -> None:
    """Position is load-bearing, so this asserts the index, not membership: a
    precedence clause has to be read before the rules it governs."""
    rules = _rules(_prompt(backstory="Return the blue kettle."))

    assert rules[0] == _PRECEDENCE_RULE, (
        "the first rule must be the precedence clause; the block currently opens with "
        f"{rules[0]!r}"
    )


@pytest.mark.parametrize(
    "rule",
    [_UNSENT_MANDATORY_REPLY_RULE, _NO_RESTART_RULE, _NEVER_MENTION_THE_FRAME_RULE],
    ids=["unsent_mandatory_reply", "no_restart_of_the_opening", "never_mention_the_frame"],
)
def test_the_rules_whose_wording_is_the_protection_are_carried_word_for_word(rule: str) -> None:
    assert rule in _rules(_prompt()), (
        "this rule's wording is what it protects — the failure it was authored against, or "
        "the frame it keeps the simulator from naming where no reply guard runs; reword it "
        "in the prompt and the cover goes with it"
    )


@pytest.mark.parametrize(
    "tool_schemas,expected",
    [(None, 12), ([{}], 16)],
    ids=["without_tools", "with_tools"],
)
def test_the_block_carries_the_rules_it_is_supposed_to(
    tool_schemas: list[dict] | None, expected: int
) -> None:
    rules = _rules(_prompt(tool_schemas=tool_schemas))

    assert (
        len(rules) == expected
    ), f"expected {expected} bulleted rules, found {len(rules)}: {rules}"


@pytest.mark.parametrize("phrase", _DROPPED_RULE_PHRASES)
def test_no_dropped_rule_survives(phrase: str) -> None:
    """Each phrase belonged to a rule dropped for contradicting authored backstories.

    Asserted against the richest fixed-text rendering — opening line, every
    rule, and the tool-guidance block — so a phrase reintroduced anywhere in
    the body is caught wherever it lands.
    """
    assert phrase not in _prompt(tool_schemas=[{}]), (
        f"{phrase!r} belongs to a rule this prompt dropped: it either contradicted what "
        "authored backstories instruct, or turned the simulator into a corrective supervisor"
    )


def test_termination_is_outcome_based_rather_than_satisfaction_gated() -> None:
    """A request the agent turned down has reached an outcome and may end.

    The rule this replaced held the conversation open until the goal was
    satisfied, so a scenario the agent correctly refuses ran to
    ``max_user_turns`` instead of ending with a gradeable transcript.
    """
    prompt = _prompt()
    stop_rule = next(rule for rule in _rules(prompt) if "###STOP###" in rule)

    assert "reached an outcome" in stop_rule, stop_rule
    assert "turned down by the agent" in stop_rule, stop_rule
    assert "the entire goal is satisfied" not in prompt, (
        "satisfaction-gated termination is back: a refused request would never reach "
        "'###STOP###'"
    )


def test_a_task_with_no_backstory_renders_no_instruction_label() -> None:
    """``UserSimulatorConfig.backstory`` defaults to ``None`` while ``mode``
    defaults to ``llm``, and a bundled project ships exactly that shape — a
    bare ``Instruction:`` label above twelve rules that keep referring to it
    would be a regression for every task in it."""
    assert "Instruction:" not in _prompt()


def test_a_backstory_is_rendered_once_between_the_opening_line_and_the_rules() -> None:
    backstory = "Ask to cancel order 51-A, then request a refund receipt."
    prompt = _prompt(backstory=backstory)

    assert prompt.count(backstory) == 1
    assert prompt.index("You are a user") < prompt.index(backstory) < prompt.index("\nRules:\n")
    assert f"\n\nInstruction: {backstory}\n" in prompt


def test_tool_guidance_is_appended_whole_and_only_when_tools_are_present() -> None:
    """The guidance block is appended after the last rule, never interleaved,
    so the no-tools rendering is a prefix of the with-tools one."""
    without_tools = _prompt()
    with_tools = _prompt(tool_schemas=[{}])

    assert "You have access to tools" not in without_tools
    assert with_tools.startswith(without_tools), (
        "the tool-guidance block must be appended after the last rule; a with-tools "
        "rendering that is not a prefix-extension of the without-tools one means the "
        "guidance was interleaved into the rules"
    )
