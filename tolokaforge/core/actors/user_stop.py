"""How a user reply ends the dialogue: which tokens stop it, and when.

The runner reads every dispatched user reply through a :class:`UserStopRule`.
The rule is built once per trial from the resolved ``actors.user`` config, so the
tokens the engine listens for and the tokens the simulator is told to send come
from one declaration.
"""

from __future__ import annotations

from dataclasses import dataclass

from tolokaforge.core.models.task_config import (
    SIMULATOR_STOP_TOKEN,
    UserSimulatorConfig,
    UserStopWithText,
    validate_stop_tokens,
)

__all__ = ["UserStop", "UserStopRule"]


@dataclass(frozen=True)
class UserStop:
    """A stop token found in a user reply.

    ``text`` is what the reply says before the token, right-stripped; empty when
    the reply is the bare token. Whatever follows the token is never delivered.
    """

    token: str
    text: str


@dataclass(frozen=True)
class UserStopRule:
    """The stop tokens a trial listens for and what text before one does."""

    tokens: tuple[str, ...] = (SIMULATOR_STOP_TOKEN,)
    with_text: UserStopWithText = "deliver"

    def __post_init__(self) -> None:
        # The same rules the config enforces, so a rule built in code cannot hold a
        # pair of tokens :meth:`find` could not tell apart.
        validate_stop_tokens(list(self.tokens))

    @classmethod
    def from_config(cls, config: UserSimulatorConfig) -> UserStopRule:
        return cls(tokens=tuple(config.stop_tokens), with_text=config.stop_with_text)

    def find(self, reply_text: str) -> UserStop | None:
        """The earliest stop token in *reply_text*, or ``None`` when it carries none.

        Earliest by position, so a reply naming two tokens stops on the one the
        model wrote first, and the text delivered is exactly what preceded it. No
        two tokens can start at one position: none contains another.
        """
        found = {
            position: token for token in self.tokens if (position := reply_text.find(token)) != -1
        }
        if not found:
            return None
        position = min(found)
        return UserStop(token=found[position], text=reply_text[:position].rstrip())
