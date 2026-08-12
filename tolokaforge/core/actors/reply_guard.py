"""User-reply guard: a generated user turn is delivered as written, or not at all.

A user-simulator reply is inspected by a list of :class:`ReplyDetector`
implementations before it reaches the agent. A reply any of them flags is
**discarded whole and regenerated** — no text is edited, excised, truncated or
substituted, so the engine has no path that can put words into a turn the model
did not write. When the attempt budget
(:data:`~tolokaforge.core.models.run_config.USER_REPLY_MAX_ATTEMPTS`) is spent,
:class:`UserReplyGuard.enforce` raises :class:`UserReplyRefused` and the trial
fails as a harness error rather than delivering a defective turn.

:class:`FourthWallDetector` covers the simulator stepping outside the customer
it plays. Its governing rule, which every pattern obeys:

    A pattern matches only when the meta-concept is attributed to a
    conversational party or to the exercise itself, *and* the noun carrying it
    heads its own phrase. A machine noun matches only bound to a first-person
    subject; an exercise noun only under a demonstrative, or in a prepositional
    frame the speaker puts itself inside; a system prompt only when possessed by
    the agent, or by the speaker with an instruction verb.

The bare noun never matches, and neither does a noun used attributively (``an
AI engineer``, ``a benchmark index fund``, ``a real person of interest``, ``your
system prompt caching feature``) — the second clause is what makes the first one
safe. ``ai``, ``model``, ``prompt``, ``benchmark``, ``simulation`` and ``llm``
are ordinary support vocabulary and are not triggers on their own: a false
positive costs a whole turn's attempt budget and then the trial, so precision
outranks recall here.

The user describing the *agent* as a machine (``"You are chatting with an
internal AI agent"``) is in frame and passes by design; only the simulator
describing *itself* is a defect.

Recall deliberately given up, because the same words carry a support meaning
often enough that the demonstrative head cannot separate them:

* ``exercise``, ``evaluation``, ``benchmark`` and ``test scenario/case/run/
  conversation`` as bare exercise nouns — ``"This exercise is not showing up in
  my activity ring."``, ``"This evaluation is taking too long."``, ``"This
  benchmark is up 4% this year."``, ``"This test case is failing after your last
  release."`` are ordinary support turns. Only the compounds (``roleplay
  exercise``, ``training exercise``, ``evaluation exercise``) survive, so a bare
  ``"This benchmark tests performance."`` is missed.
* the prepositional frame without a first-person subject —
  ``"During the simulation, the app froze and I lost my mesh."`` is what a
  customer of simulation software says, so the frame only matches when the
  speaker places itself inside it (``"In this simulation, I play the role of a
  customer."``). A break phrased about a third party, ``"During the simulation
  the agent refused twice."``, is missed.
* ``instructions`` in every position — ``"My instructions say to take two
  tablets daily."`` is a support turn far more often than a leaked persona.
* ``"I'm an LLM-based assistant."`` — the hyphen makes the machine noun
  attributive, which is the anchor doing its job on a genuine break.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models.run_config import USER_REPLY_MAX_ATTEMPTS
from tolokaforge.core.models.trajectory import ReplyDefect

if TYPE_CHECKING:
    from tolokaforge.core.llm.client import GenerationResult

__all__ = [
    "DEFAULT_REPLY_DETECTORS",
    "FourthWallDetector",
    "ReplyDetector",
    "UserReplyGuard",
    "UserReplyRefused",
]

_logger = get_logger("user_reply_guard")


_MACHINE_NOUN = (
    r"(?:large\s+language\s+model|language\s+model|virtual\s+assistant"
    r"|AI\s+assistant|AI\s+agent|chatbot|A\.I\.|AI\s+(?:model|system|bot|program)|AI(?!-)|LLM)"
)
_EXERCISE_NOUN = (
    r"(?:simulation|simulated\s+\w+|roleplay|role[-\s]play"
    r"|(?:roleplay|role[-\s]play|training|evaluation)\s+exercise)"
)
_CLOSE = r"[.,;:!?)\"']"

# Every family needs the same guarantee — the noun must HEAD its phrase, not
# modify a following noun — but the legal continuations differ, so there are
# three anchors rather than one.
#   _EXERCISE_END allows `of`: "this is a simulation OF the task" is a hit.
_EXERCISE_END = (
    rf"(?=\s*(?:{_CLOSE}|$)|\s+(?:so|and|but|then|right|okay|ok|of|for|where|which"
    r"|that|to|I|we|you|is|was|isn't)\b)"
)
#   _MACHINE_END forbids a following bare noun ("I'm an AI engineer", "your
#   system prompt caching feature") but allows participles, which modify the
#   noun rather than replace it ("a chatbot following a script"). The system
#   prompt family shares it: both are nouns a support turn uses attributively.
_MACHINE_END = (
    rf"(?=\s*(?:{_CLOSE}|$)|\s+(?:and|but|so|then|too|here|now|really|honestly|unfortunately"
    r"|I|my|you|your|it|its|this|that|who|which|we|they|am|are|is|was|were|can|cannot|can't"
    r"|don't|do|does|doesn't|have|has|had|will|would|following|designed|trained|built"
    r"|programmed|created|running|responding|talking|speaking)\b)"
)
#   _HUMAN_END excludes `of`/`for`: "not a real person OF interest" is a fraud ticket.
_HUMAN_END = rf"(?=\s*(?:{_CLOSE}|$)|\s+(?:and|but|so|then|here|I|it|this|that|you|we|they)\b)"

_FOURTH_WALL_PATTERNS: tuple[tuple[str, str], ...] = (
    # family 1 — the speaker identifies itself as a machine
    (
        "self_identified_as_model",
        rf"\bI(?:'m| am| was)\s+(?:just\s+|only\s+|merely\s+)?(?:an?\s+)?"
        rf"(?:\w+[\s-]+){{0,2}}?{_MACHINE_NOUN}\b{_MACHINE_END}",
    ),
    (
        "self_identified_as_model",
        rf"\bas\s+an?\s+(?:\w+[\s-]+){{0,2}}?{_MACHINE_NOUN}\b\s*,?\s+(?:I\b|my\b)",
    ),
    (
        "denied_being_human",
        rf"\bI(?:'m| am)\s+not\s+(?:a\s+)?(?:real|actual|human|genuine)\s+"
        rf"(?:person|customer|user|human|caller)\b{_HUMAN_END}",
    ),
    # family 2 — the exercise named as an exercise, or a party's prompt named
    (
        "named_the_exercise",
        rf"\bthis\s+(?:conversation\s+|chat\s+|call\s+)?(?:is|was)\s+"
        rf"(?:just\s+|only\s+|merely\s+)?an?\s+"
        rf"{_EXERCISE_NOUN}(?:\s+{_EXERCISE_NOUN})?\b{_EXERCISE_END}",
    ),
    (
        "named_the_exercise",
        rf"\bthis\s+{_EXERCISE_NOUN}\s+(?:is|was|tests|measures|scores|ends|starts|matters)\b",
    ),
    # The prepositional frame is where a customer describes what went wrong
    # ("during the simulation, the app froze"), so an end anchor cannot carry
    # this one — a comma satisfies it. What separates the break is the speaker
    # placing *itself* inside the exercise.
    (
        "named_the_exercise",
        r"\b(?:in|for|during)\s+(?:this|the)\s+"
        r"(?:simulation|benchmark|roleplay|role[-\s]play|evaluation\s+run)\b"
        r"\s*,?\s+(?:I|we)\b",
    ),
    # Two ways the agent's prompt heads its phrase: the general anchor, or a
    # verb reciting what the prompt says — the latter is the family's least
    # ambiguous break and no anchor built for nouns can see it.
    (
        "named_a_party_prompt",
        rf"\b(?:your|its|the\s+agent's)\s+system\s+prompt\b"
        rf"(?:{_MACHINE_END}"
        r"|(?=\s+(?:say|says|said|tells?|told|instructs?|instructed"
        r"|states?|forbids?|requires?)\b))",
    ),
    (
        "named_a_party_prompt",
        r"\bmy\s+system\s+prompt\s+(?:say|says|said|tells?|instructs?|states?)\b",
    ),
    (
        "named_own_instructions",
        r"\bmy\s+(?:backstory|persona)\s+(?:say|says|said|state|states|tell|tells)\b",
    ),
    (
        "named_own_instructions",
        r"\bthe\s+(?:scenario|backstory|persona)\s+(?:say|says|said|states|describes)\b",
    ),
)

_FOURTH_WALL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE)) for reason, pattern in _FOURTH_WALL_PATTERNS
)


@runtime_checkable
class ReplyDetector(Protocol):
    """Contract for one inspection a generated user reply must survive."""

    name: str

    def inspect(self, text: str) -> ReplyDefect | None:
        """Return the defect found in *text*, or ``None`` when it is clean.

        The returned ``detector`` is advisory: :class:`UserReplyGuard` records
        the name the detector is registered under. ``excerpt`` may be any
        length — :class:`ReplyDefect` bounds it.
        """
        ...


class FourthWallDetector:
    """The simulator talking about itself as a machine, or about the exercise."""

    name = "fourth_wall"

    def inspect(self, text: str) -> ReplyDefect | None:
        for reason, pattern in _FOURTH_WALL_RULES:
            match = pattern.search(text)
            if match is not None:
                return ReplyDefect(detector=self.name, reason=reason, excerpt=match.group(0))
        return None


def _reason_codes(rejected: Sequence[ReplyDefect]) -> list[str]:
    """The discarded attempts as ``detector:reason`` codes, never their text.

    Everything the guard surfaces outside its own ``WARNING`` lines — the
    refusal message, an exception note, the refusal log's ``reasons`` — is built
    from this, so no rejected reply can reach a consumer that reads prose.
    """
    return [f"{defect.detector}:{defect.reason}" for defect in rejected]


DEFAULT_REPLY_DETECTORS: tuple[ReplyDetector, ...] = (FourthWallDetector(),)
"""The detectors every :class:`UserReplyGuard` runs unless told otherwise.

Tuple order is inspection order — the first detector to flag a reply owns it.
The instances are built once at import and shared by every guard, so a detector
registered here carries no per-trial state."""


class UserReplyRefused(RuntimeError):
    """Every generation of one user turn broke frame.

    The message names the detectors, the reason codes and the attempt count and
    **never quotes the rejected reply**. ``classify_loop_error`` has a prose
    tier keyed on provider names appearing in an exception's text, so a reply
    that happened to discuss one would re-attribute this harness defect to the
    provider and move the trial out of the measured denominator. The rejected
    text is evidence and lives in the ``WARNING`` log lines instead.
    """

    def __init__(self, rejected: tuple[ReplyDefect, ...]) -> None:
        self.rejected = rejected
        codes = ", ".join(_reason_codes(rejected))
        super().__init__(
            f"User simulator broke frame on all {len(rejected)} generation attempts "
            f"({codes}); the turn is refused rather than delivered. The rejected text "
            "is in this trial's guard log lines, deliberately not in this message."
        )


class UserReplyGuard:
    """Runs the detectors over each generated reply and regenerates on a hit."""

    def __init__(
        self,
        detectors: Sequence[ReplyDetector] = DEFAULT_REPLY_DETECTORS,
        *,
        max_attempts: int = USER_REPLY_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(
                f"UserReplyGuard needs at least one generation attempt, got {max_attempts}; "
                "a guard that never generates would refuse every user turn."
            )
        self.detectors = tuple(detectors)
        self.max_attempts = max_attempts

    def enforce(
        self,
        generate: Callable[[], GenerationResult],
        *,
        log_extra: dict[str, str] | None = None,
    ) -> tuple[GenerationResult, tuple[ReplyDefect, ...]]:
        """Return the first clean generation, paired with the defects discarded before it.

        Calls *generate* up to :attr:`max_attempts` times, running the detectors
        in registration order and taking the first hit, so one discarded attempt
        yields exactly one defect. Raises :class:`UserReplyRefused` when the
        budget is spent.

        *log_extra* is stamped on every log line this call emits. The guard logs
        under its own logger name, so without it the caller's trial identity —
        the only thing that ties a discarded reply to the trial that paid for it
        — is absent from the record.

        A *generate* failure after one or more discards re-raises with the
        discarded reason codes attached as a note: the exception is what the
        trial reports, and those attempts are otherwise lost with the call.
        """
        rejected: list[ReplyDefect] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = generate()
            except Exception as exc:
                if rejected:
                    exc.add_note(
                        f"{len(rejected)} user reply/replies were discarded for broken "
                        f"frame before this error: {', '.join(_reason_codes(rejected))}"
                    )
                raise
            defect = self._inspect(result.text)
            if defect is None:
                return result, tuple(rejected)
            rejected.append(defect)
            _logger.warning(
                "User simulator broke frame; discarding the reply and regenerating",
                context=log_extra,
                detector=defect.detector,
                reason=defect.reason,
                excerpt=defect.excerpt,
                attempt=attempt,
                max_attempts=self.max_attempts,
            )
        _logger.error(
            "User simulator broke frame on every attempt; refusing the turn",
            context=log_extra,
            reasons=_reason_codes(rejected),
            attempts=self.max_attempts,
        )
        raise UserReplyRefused(tuple(rejected))

    def _inspect(self, text: str) -> ReplyDefect | None:
        """First hit in registration order, recorded under the registered name.

        A detector's own ``detector`` field is overwritten here, so what the
        bundle groups on is the registration a run can be read back from rather
        than a string an implementation chose.
        """
        for detector in self.detectors:
            defect = detector.inspect(text)
            if defect is not None:
                return defect.model_copy(update={"detector": detector.name})
        return None
