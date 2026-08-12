r"""A leaked reasoning delimiter is not a user turn.

A reasoning model writes its planning prose into a channel the provider's chat
template is supposed to consume. When that parse is imperfect the delimiter
survives into ``content`` — a bare ``</think>`` ahead of the reply, or a whole
``<think>…</think>`` span — and the plan text is recorded as the customer's
turn. :class:`ScratchpadDetector` flags the delimiter, and
:class:`~tolokaforge.core.actors.reply_guard.UserReplyGuard` discards the reply
whole and regenerates.

The tag is **not** stripped here. The user path can ask again, and a defect
curable by regenerating must not be cured by editing the words the model wrote.

The rule, which is the anchor:

    A think tag matches only at a structural position — beginning the reply, or
    beginning a line.

Every real leak is structural: the delimiter is emitted at a channel boundary,
never mid-sentence. Anchoring on the start of the *string* instead would miss
the shape the measurement reports as dominant — planning prose followed by a
lone ``</think>`` on its own line and then the reply.

What this does not cover:

* **The untagged half, which is the larger one.** Planning prose carrying no
  delimiter at all is not separable from ordinary support English by any
  pattern set: five families were built against it and each cleared the round
  it was narrowed against and fell to the next, because the distinction is
  stance rather than surface form — a scratchpad talks *about* the persona, and
  the same words in the same structure are what a customer says about their own
  assigned business role, the message they are composing, or their persona
  field. Measured and filed as #1095; the backstop is the simulator's own
  prompt rule.
* **A tag mentioned mid-line, by design.** ``"My parser chokes on </think>
  tags in the streamed output"`` is a support ticket, and it is the anchor that
  keeps those clean.
* **A pasted multi-line log is a false positive** when its quoted content
  starts a line with a think tag — ``"Here is the log you asked for:\n<think>\n…"``
  and ``"The output file starts with\n</think>\nwhich breaks my parser."`` both
  fire. It is rare (this text is simulator-generated, not arbitrary human
  input), it costs a turn's attempt budget and then the trial, and it fails
  *loudly*, carrying the matched excerpt, rather than silently. The alternative
  anchor would miss the dominant leak shape, so the trade is taken knowingly.
* **The agent path**, which carries the identical leak into ``trajectory.yaml``
  and the judge's evidence with no defense and cannot regenerate — re-rolling
  an agent turn re-rolls the thing being measured. Stripping belongs there,
  in ``AssistantTextPolicy``; filed as #1094.
"""

from __future__ import annotations

import re

from tolokaforge.core.models.trajectory import ReplyDefect

__all__ = ["ScratchpadDetector"]

_THINK_TAG = re.compile(r"^[ \t]*</?think\s*>", re.IGNORECASE | re.MULTILINE)


class ScratchpadDetector:
    """The model's reasoning delimiter surviving into the reply text."""

    name = "scratchpad"

    def inspect(self, text: str) -> ReplyDefect | None:
        match = _THINK_TAG.search(text)
        if match is None:
            return None
        return ReplyDefect(detector=self.name, reason="think_tag", excerpt=match.group(0))
