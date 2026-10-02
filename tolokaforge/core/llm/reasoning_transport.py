"""Where a provider puts reasoning on a response, and how to read it anyway.

A preset picks one reasoning codec, by glob, before the model has said
anything. The model's behaviour is not that stable: five probes of
``openai/o4-mini`` on 2026-10-02 returned readable ``reasoning_content`` once
and an opaque blob four times, on the same slug through the same route. A
static choice cannot be right about that, and every newly added model arrives
with the choice unmade — usually by someone fixing an unrelated axis.

So reading is separated from replaying. :func:`arriving_reasoning` says what a
response actually carried, and :class:`PermissiveReasoningReader` builds
:class:`~tolokaforge.core.llm.reasoning.StructuredReasoning` out of whichever
channel is present. The client falls back to it when the preset's codec
extracted nothing, so reasoning the provider sent is kept regardless of which
codec a glob happened to install.

**Reading is wire-neutral.** ``_convert_messages`` splices replay payloads with
``self.capabilities.reasoning_codec.encode_for_replay`` — the *preset's* codec,
never whatever read the reasoning — so extracting more changes nothing about
what is sent. Replay is where the vendor rules live (OpenAI refuses echoed
reasoning; Gemini's placeholder blocks poison later turns) and a wrong answer
there breaks requests rather than merely losing data. This module does not
touch it.

``scripts/analysis/probe_reasoning_transport.py`` imports from here, so the
audit and the engine answer "where can reasoning live" from one definition
rather than two that drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning

__all__ = [
    "READABLE_DETAIL_TYPES",
    "ArrivingReasoning",
    "PermissiveReasoningReader",
    "arriving_reasoning",
]

#: ``reasoning_details`` entry types whose payload a later turn could read.
#: ``reasoning.encrypted`` is deliberately absent: an opaque blob is not
#: deliberation anyone can keep, and counting it would report a loss where
#: none is possible.
READABLE_DETAIL_TYPES: frozenset[str] = frozenset({"reasoning.text", "reasoning.summary"})


@dataclass(frozen=True)
class ArrivingReasoning:
    """What one response carried, before any codec looked at it.

    ``readable`` and ``opaque`` are separate fields rather than entries in one
    list because the two answer different questions and a caller that conflates
    them reports a loss on every encrypted-only call — which is most calls on
    some OpenAI routes, where keeping nothing is correct.
    """

    readable: tuple[str, ...] = ()
    """Channel names carrying text a later turn could act on, canonical first."""

    opaque: bool = False
    """Whether an encrypted ``reasoning_details`` payload was present."""

    @property
    def anything(self) -> bool:
        """Whether reasoning arrived at all, readable or not."""
        return bool(self.readable) or self.opaque


def arriving_reasoning(response_message: Any) -> ArrivingReasoning:
    """Inspect a litellm response message for the channels reasoning uses.

    Pure attribute reads, so the engine can afford it on every call and the
    probe can run it offline against a recorded response. Never raises: a
    malformed or mocked message reads as "nothing arrived" rather than taking
    down the turn that carried it.
    """
    readable: list[str] = []
    if getattr(response_message, "reasoning_content", None):
        readable.append("reasoning_content")

    opaque = False
    psf = getattr(response_message, "provider_specific_fields", None) or {}
    if isinstance(psf, dict):
        if psf.get("reasoning"):
            readable.append("reasoning")
        details = psf.get("reasoning_details") or []
        if isinstance(details, list):
            if any(isinstance(d, dict) and d.get("type") in READABLE_DETAIL_TYPES for d in details):
                readable.append("reasoning_details")
            elif details:
                opaque = True
    return ArrivingReasoning(readable=tuple(readable), opaque=opaque)


class PermissiveReasoningReader:
    """Last-resort reader: keeps whatever readable channel a response used.

    Deliberately not a :class:`~tolokaforge.core.llm.reasoning_codec.ReasoningCodec`
    — it only reads. It is reached when the preset's codec extracted nothing
    from a response that did carry readable reasoning, which is a statement
    about the preset, not about the provider.

    Unlike a vendor codec it tolerates anything. ``GeminiReasoningCodec.extract``
    raises on an unfamiliar ``reasoning_details`` type, which is right for a
    codec that must round-trip a shape and wrong for a reader whose only job is
    to not lose text.
    """

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        blocks: list[ReasoningBlock] = []

        content = getattr(response_message, "reasoning_content", None)
        if isinstance(content, str) and content.strip():
            blocks.append(ReasoningBlock(type="summary_text", text=content))

        psf = getattr(response_message, "provider_specific_fields", None) or {}
        if isinstance(psf, dict):
            mirror = psf.get("reasoning")
            if isinstance(mirror, str) and mirror.strip() and not blocks:
                # The OpenRouter mirror repeats ``reasoning_content`` on routes
                # that fill both; only read it when nothing else did.
                blocks.append(ReasoningBlock(type="summary_text", text=mirror))

            for detail in psf.get("reasoning_details") or []:
                if not isinstance(detail, dict):
                    continue
                if detail.get("type") not in READABLE_DETAIL_TYPES:
                    continue
                text = detail.get("text") or detail.get("summary")
                if isinstance(text, str) and text.strip():
                    blocks.append(ReasoningBlock(type="thinking", text=text))

        if not blocks:
            return None
        # ``capture_only``: these blocks were read, not extracted by the codec
        # that will be asked to replay them, so they must never reach the wire.
        return StructuredReasoning(blocks=tuple(blocks), capture_only=True)
