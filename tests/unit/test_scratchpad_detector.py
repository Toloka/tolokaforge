r"""A leaked reasoning delimiter is a defect; a customer writing about one is not.

Pass rows and detect rows are one corpus, because the pair that decides the
rule — a tag beginning a line versus the same tag inside a sentence — is only
readable side by side.

The falsifying patches, both measured against the tables below:

* dropping the ``^[ \t]*`` anchor (leaving ``</?think\s*>``) reddens four of the
  six ``MUST_PASS`` rows — every row carrying a literal tag mid-line.
* dropping the ``\s*>`` tail (leaving ``^[ \t]*</?think``) reddens the
  ``<thinking>`` row, the only one whose line-leading text starts a longer word.
* anchoring on the start of the *string* instead of the start of a *line*
  (dropping ``re.MULTILINE``) loses ``MUST_DETECT`` row 2, the bare closing tag
  the measurement reports as the dominant leak shape.

``The error is <500ms latency> in the log`` is the angle-bracket control: it
carries no tag at all, so no patch to this pattern reddens it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_user_reply_guard import MUST_PASS as FOURTH_WALL_MUST_PASS
from tests.utils.project_fixtures import is_lfs_pointer
from tolokaforge.core.actors.reply_guard import FourthWallDetector
from tolokaforge.core.actors.scratchpad import ScratchpadDetector

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

MUST_DETECT = [
    "</think>\n\nHi, my flight AX221 got cancelled and I need to rebook today.",
    (
        "The user wants me to act as a passenger whose flight was cancelled.\n"
        "</think>\nHi, my flight AX221 got cancelled."
    ),
    "<think>The user wants me to play a customer.</think>Hi, my flight got cancelled.",
    (
        "<think>The user wants me to act as a customer with a billing problem. "
        "I'll open by asking about the charge."
    ),
    "<think>plan</think>\nThe user wants me to play a caller who lost their debit card.",
    "   <think>\nLet me plan the opening.\n</think>\nHi, I need help.",
]

MUST_PASS = [
    "My parser chokes on </think> tags in the streamed output, can you fix it?",
    "The template renders <think> and </think> literally in my logs.",
    "The error is <500ms latency> in the log, is that normal?",
    "Does your API strip <think> blocks automatically?",
    "I see the string </think> in the middle of every response body.",
    "My prompt template emits this:\n<thinking> blocks should be hidden but they are not.",
]

_AUTHORED_USER_KEYS = ("initial_user_message", "actors.user.backstory")


def _declared(document: dict[str, Any], dotted_key: str) -> Any:
    """The value at *dotted_key*, or ``None`` where the pack declares no such key."""
    node: Any = document
    for key in dotted_key.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _committed_user_turns() -> list[str]:
    """Every USER turn in the committed trajectory bundles that are hydrated.

    A bundle left as an LFS pointer costs the turns in its own file and nothing
    else — the hydrated bundles are still swept — so only a set with no
    hydrated bundle in it has nothing left to inspect. A glob that matches
    nothing at all is not that case, and reaches the caller's emptiness assert.
    """
    bundles = sorted(REPO_ROOT.glob("tests/**/trajectory.yaml"))
    hydrated = [path for path in bundles if not is_lfs_pointer(path)]
    if bundles and not hydrated:
        pytest.skip("Every committed trajectory is an LFS pointer — run 'git lfs pull' first")

    turns: list[str] = []
    for path in hydrated:
        messages = yaml.safe_load(path.read_text(encoding="utf-8"))["messages"]
        turns.extend(m["content"] for m in messages if m["role"] == "user" and m["content"].strip())
    return turns


def _authored_user_strings() -> list[str]:
    """Every pinned opener and user backstory the bundled example packs declare."""
    strings: list[str] = []
    for path in sorted(REPO_ROOT.glob("examples/**/task.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared = (_declared(document, key) for key in _AUTHORED_USER_KEYS)
        strings.extend(v for v in declared if isinstance(v, str) and v.strip())
    return strings


class TestTheScratchpadCorpus:
    @pytest.mark.parametrize("text", MUST_DETECT)
    def test_a_delimiter_at_a_structural_position_is_detected(self, text: str) -> None:
        detector = ScratchpadDetector()

        defect = detector.inspect(text)

        assert defect is not None
        assert defect.detector == detector.name
        assert defect.reason == "think_tag"
        assert defect.excerpt in text

    @pytest.mark.parametrize("text", MUST_PASS)
    def test_a_customer_writing_about_a_tag_passes(self, text: str) -> None:
        assert ScratchpadDetector().inspect(text) is None

    def test_the_excerpt_carries_the_text_that_followed_the_tag(self) -> None:
        """A bare tag is the same handful of characters in a leak and in a
        pasted log, so the excerpt runs from the tag to the bound
        ``ReplyDefect`` holds — that trailing text is what a reader of the
        ``WARNING`` line has to tell the two apart with."""
        text = "The user wants me to act as a passenger.\n</think>\nHi, my flight was cancelled."

        defect = ScratchpadDetector().inspect(text)

        assert defect is not None
        assert defect.excerpt == "</think>\nHi, my flight was cancelled."


class TestTheTwoDetectorsAreDisjointOnTheCorpus:
    """Neither family claims the other's rows, so appending the registration
    cannot move a reason code either detector already records."""

    @pytest.mark.parametrize("text", MUST_DETECT)
    def test_the_fourth_wall_detector_claims_no_leaked_delimiter(self, text: str) -> None:
        assert FourthWallDetector().inspect(text) is None

    @pytest.mark.parametrize("text", MUST_PASS)
    def test_the_fourth_wall_detector_claims_no_support_ticket_about_tags(self, text: str) -> None:
        assert FourthWallDetector().inspect(text) is None


class TestNothingCommittedToThisRepoReadsAsAScratchpad:
    """The negative corpus is swept live rather than transcribed into a table:
    a transcription goes stale against the files it is a copy of, and every one
    of these strings is a real user-facing sentence this repo already ships.

    Each sweep asserts its corpus is non-empty first — a glob that stops
    matching must fail here rather than pass with nothing to inspect.
    """

    def test_no_fourth_wall_pass_row_is_read_as_a_scratchpad(self) -> None:
        assert FOURTH_WALL_MUST_PASS

        detector = ScratchpadDetector()
        assert [t for t in FOURTH_WALL_MUST_PASS if detector.inspect(t) is not None] == []

    def test_no_committed_user_turn_is_read_as_a_scratchpad(self) -> None:
        turns = _committed_user_turns()
        assert turns

        detector = ScratchpadDetector()
        assert [t for t in turns if detector.inspect(t) is not None] == []

    def test_no_authored_task_string_is_read_as_a_scratchpad(self) -> None:
        strings = _authored_user_strings()
        assert strings

        detector = ScratchpadDetector()
        assert [t for t in strings if detector.inspect(t) is not None] == []
