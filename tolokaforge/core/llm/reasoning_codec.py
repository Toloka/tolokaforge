"""Provider-specific reasoning extraction + replay.

The :class:`ReasoningCodec` Protocol abstracts the two provider-specific
operations needed around thinking/reasoning:

1. **Extract** a :class:`StructuredReasoning` from a raw provider response
   message (``response.choices[0].message``).
2. **Encode for replay** — produce the *provider-shaped* extra fields that
   must be merged into an assistant message dict on the next turn so the
   provider can verify its own signed thinking blocks.

Concrete codecs (:class:`AnthropicReasoningCodec`, :class:`OpenAIReasoningCodec`)
are registered via the preset system (see :mod:`tolokaforge.core.llm.presets`).
The default :class:`NoReasoningCodec` is a no-op used by providers that do
not surface reasoning.

Canonical litellm message surface (v1.83+):

* ``message.reasoning_content: str`` — summary for all providers
* ``message.thinking_blocks: list[dict]`` — structured thinking for Anthropic,
  each entry one of ``{type: "thinking", thinking: str, signature: str}`` or
  ``{type: "redacted_thinking", data: str}``
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning

__all__ = [
    "ReasoningCodec",
    "NoReasoningCodec",
    "AnthropicReasoningCodec",
    "OpenAIReasoningCodec",
    "GeminiReasoningCodec",
    "QwenReasoningCodec",
]


@runtime_checkable
class ReasoningCodec(Protocol):
    """Extract + replay contract for provider-specific reasoning shapes."""

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        """Build a :class:`StructuredReasoning` from a raw provider message.

        Returns ``None`` when the response carries no reasoning data.
        Implementations MUST raise on malformed block shapes rather than
        silently dropping data (per AGENTS.md "surface failures" rule).
        """

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:
        """Return provider-shaped extra fields to merge into an assistant message dict.

        For litellm, these fields are set as first-class keys on the message
        dict (e.g. ``{"role": "assistant", "content": "…", "thinking_blocks": [...]}``)
        and forwarded untouched to the provider.
        """


class NoReasoningCodec:
    """Default no-op codec: no reasoning surfaced, nothing to replay."""

    def extract(self, response_message: Any) -> StructuredReasoning | None:  # noqa: ARG002
        return None

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:  # noqa: ARG002
        return {}


def _dedup_summary(blocks: tuple[ReasoningBlock, ...], summary: str | None) -> str | None:
    """Drop ``summary`` when it's a 1:1 mirror of the joined block text.

    Plan B from the post-PR-#88 trim: ``StructuredReasoning.summary`` is
    reserved for the legitimate *server-shipped abstract* case (a string
    distinct from — typically shorter than — the verbatim thinking).
    OpenRouter and OpenAI's adapter both populate ``summary`` with the
    same content the blocks already carry, so storing both doubles the
    on-disk reasoning footprint without adding signal. When that
    duplicate is detected, drop ``summary``; ``StructuredReasoning.as_plain_text``
    still recovers the content from the blocks.

    Mirrors the joining rule used by ``StructuredReasoning.as_plain_text``
    so that the dedup decision matches the canonical projection.
    """
    if summary is None:
        return None
    joined = "\n\n".join(b.text for b in blocks if b.text)
    if joined == summary:
        return None
    return summary


class AnthropicReasoningCodec:
    """Extracts / replays thinking blocks for Anthropic-family models.

    Two transports surface structured Anthropic reasoning differently:

    * **Direct Anthropic API** (litellm canonical): ``thinking_blocks`` —
      a list of ``{type:"thinking",thinking:str,signature:str}`` /
      ``{type:"redacted_thinking",data:str}`` dicts.
    * **OpenRouter routing**: ``provider_specific_fields.reasoning_details``
      — a list of ``{type:"reasoning.text",text:str,format:str,signature:str}``
      dicts. ``thinking_blocks`` is **None** on this transport (verified by
      live probe of ``openrouter/anthropic/claude-opus-4.7`` 2026-04-27).

    The codec normalises both into the same :class:`StructuredReasoning`
    shape so downstream consumers stay routing-agnostic.

    Contract:

    * ``thinking_blocks`` wins when populated (it's the canonical
      Anthropic surface and the only one that survives litellm's
      transport-agnostic adapter).
    * Otherwise, ``provider_specific_fields.reasoning_details`` populates
      the blocks. The signature on each detail item is preserved verbatim
      — the bytes the model needs echoed back for interleaved-thinking
      continuity.
    * ``reasoning_content`` always seeds ``StructuredReasoning.summary``
      regardless of which transport carried the structured shape.
    * Unknown block types / non-dict entries raise ``ValueError`` — we do
      not silently drop what we cannot interpret.
    """

    _ALLOWED_NATIVE_TYPES = frozenset({"thinking", "redacted_thinking"})
    _OPENROUTER_TEXT_TYPE = "reasoning.text"

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        thinking_blocks = getattr(response_message, "thinking_blocks", None) or []
        summary = getattr(response_message, "reasoning_content", None) or None
        reasoning_details = self._reasoning_details(response_message)

        if not thinking_blocks and not reasoning_details and not summary:
            return None

        if thinking_blocks:
            blocks = tuple(self._native_to_block(raw) for raw in thinking_blocks)
            transport = "anthropic_native"
        elif reasoning_details:
            blocks = tuple(self._openrouter_to_block(raw) for raw in reasoning_details)
            transport = "openrouter"
        else:
            blocks = ()
            transport = None
        return StructuredReasoning(
            blocks=blocks,
            summary=_dedup_summary(blocks, summary),
            transport=transport,
        )

    @staticmethod
    def _reasoning_details(response_message: Any) -> list[Any]:
        """Pull ``provider_specific_fields.reasoning_details`` defensively.

        ``provider_specific_fields`` may itself be ``None``; the reasoning
        list may also be missing or non-list. Returns ``[]`` in any of
        those cases.
        """
        psf = getattr(response_message, "provider_specific_fields", None)
        if psf is None:
            return []
        if isinstance(psf, dict):
            details = psf.get("reasoning_details")
        else:
            details = getattr(psf, "reasoning_details", None)
        if isinstance(details, list):
            return details
        return []

    def _native_to_block(self, raw: Any) -> ReasoningBlock:
        if not isinstance(raw, dict):
            raise ValueError(f"Anthropic thinking block must be dict, got {type(raw).__name__}")
        block_type = raw.get("type")
        if block_type not in self._ALLOWED_NATIVE_TYPES:
            allowed = sorted(self._ALLOWED_NATIVE_TYPES)
            raise ValueError(
                f"Unknown Anthropic block type: {block_type!r}; expected one of {allowed}"
            )
        if block_type == "thinking":
            return ReasoningBlock(
                type="thinking",
                text=raw.get("thinking", "") or "",
                signature=raw.get("signature"),
            )
        # redacted_thinking — no signature, opaque payload only
        return ReasoningBlock(
            type="redacted_thinking",
            text="",
            encrypted_data=raw.get("data"),
        )

    def _openrouter_to_block(self, raw: Any) -> ReasoningBlock:
        if not isinstance(raw, dict):
            raise ValueError(
                f"OpenRouter reasoning_details entry must be dict, got {type(raw).__name__}"
            )
        block_type = raw.get("type")
        if block_type != self._OPENROUTER_TEXT_TYPE:
            raise ValueError(
                f"Unknown OpenRouter reasoning_details type: {block_type!r}; "
                f"expected {self._OPENROUTER_TEXT_TYPE!r}"
            )
        return ReasoningBlock(
            type="thinking",
            text=raw.get("text", "") or "",
            signature=raw.get("signature"),
        )

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:
        """Emit the provider-shaped replay payload matching ``reasoning.transport``.

        - ``anthropic_native`` (and the legacy ``None`` default for backward-
          compat with already-stored trajectories) → ``thinking_blocks``.
        - ``openrouter`` → ``reasoning_details`` (OpenRouter's per-message
          field for round-tripping signed reasoning).
        """
        if not reasoning.blocks:
            return {}
        if reasoning.transport == "openrouter":
            return {
                "reasoning_details": [
                    self._block_to_openrouter_dict(block) for block in reasoning.blocks
                ],
            }
        return {
            "thinking_blocks": [self._block_to_native_dict(block) for block in reasoning.blocks],
        }

    def _block_to_native_dict(self, block: ReasoningBlock) -> dict[str, Any]:
        if block.type == "thinking":
            out: dict[str, Any] = {"type": "thinking", "thinking": block.text}
            if block.signature is not None:
                out["signature"] = block.signature
            return out
        if block.type == "redacted_thinking":
            out = {"type": "redacted_thinking"}
            if block.encrypted_data is not None:
                out["data"] = block.encrypted_data
            return out
        raise ValueError(f"Cannot encode block type {block.type!r} for Anthropic replay")

    def _block_to_openrouter_dict(self, block: ReasoningBlock) -> dict[str, Any]:
        if block.type != "thinking":
            raise ValueError(
                f"OpenRouter reasoning_details only carries 'reasoning.text' "
                f"entries; cannot encode block type {block.type!r}"
            )
        out: dict[str, Any] = {
            "type": self._OPENROUTER_TEXT_TYPE,
            "text": block.text,
            "format": "anthropic-claude-v1",
        }
        if block.signature is not None:
            out["signature"] = block.signature
        return out


class OpenAIReasoningCodec:
    """Extracts reasoning summary for OpenAI-family models.

    OpenAI surfaces reasoning as a single ``reasoning_content`` string; no
    structured blocks, no signatures. Replay is a no-op — OpenAI does not
    accept echoed reasoning on subsequent turns.
    """

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        reasoning_content = getattr(response_message, "reasoning_content", None) or None
        if not reasoning_content:
            return None
        # The codec's only data source is ``reasoning_content`` — putting it
        # in both ``blocks[0].text`` and ``summary`` would double the on-disk
        # footprint without adding signal. Store it once on the block;
        # ``StructuredReasoning.as_plain_text`` is the canonical projection.
        block = ReasoningBlock(type="summary_text", text=reasoning_content)
        return StructuredReasoning(
            blocks=(block,),
            summary=_dedup_summary((block,), reasoning_content),
        )

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:  # noqa: ARG002
        return {}


class GeminiReasoningCodec:
    """Extracts / replays reasoning for Google Gemini family over OpenRouter.

    Gemini's reasoning surface uses the same OpenRouter
    ``provider_specific_fields.reasoning_details`` envelope as Anthropic-via-
    OpenRouter, but with two key differences:

    * **Two block types**:

      - ``reasoning.text`` (Gemini Pro lineage) — readable thinking
        text. Mapped to :class:`ReasoningBlock` (``type="thinking"``,
        ``text=<verbatim>``).
      - ``reasoning.encrypted`` (Gemini Flash lineage) — opaque
        payload, no readable text. The provider returns a single
        ``data`` field carrying base64-shaped bytes. Mapped to
        :class:`ReasoningBlock` (``type="redacted_thinking"``,
        ``text=""``, ``encrypted_data=<bytes>``) — same semantic as
        Anthropic's redacted_thinking.

    * **No signatures, no format**: Gemini does NOT emit a per-block
      ``signature`` and does NOT emit a ``format`` field. The codec must
      tolerate their absence; replay carries the same shape back.

    Replay strategy: emit the original ``reasoning_details`` envelope as a
    top-level message field. OpenRouter forwards it untouched to Google
    so the model retains reasoning-context continuity across turns.

    Loud-fail discipline: unknown block types raise :class:`ValueError`
    rather than silently dropping data.
    """

    _TEXT_TYPE = "reasoning.text"
    _ENCRYPTED_TYPE = "reasoning.encrypted"
    # OpenRouter returns a constant 48-char base64 blob (UUID
    # ``e24830a7-5cd6-42fe-998b-ee539e72b9c3``) as the ``data`` field
    # of a ``reasoning.encrypted`` block when Gemini emitted no real
    # thinking on a turn (e.g. a tool-call follow-up where the model
    # already had context from the previous turn). Real opaque blobs
    # are 1000-3000+ base64 chars. When the placeholder-drop guard is
    # enabled, ``encode_for_replay`` drops anything shorter than this
    # threshold so the placeholder doesn't waste prompt tokens on
    # subsequent turns and doesn't create few-shot patterns the model
    # echoes back. Detection is length-based (not exact-match against
    # the constant UUID) to stay robust against future OpenRouter
    # changes to the placeholder string. ``extract`` does NOT filter
    # — we still record the placeholder in ``trajectory.yaml`` so the
    # on-disk artifact reflects what the API actually returned.
    #
    # Toggle via ``drop_placeholder_signature`` on construction. The
    # 2026-04-30 PR #101 default was unconditional drop. The 2026-05-21
    # eval surfaced strong evidence that Gemini 3.1 Pro stops thinking
    # past turn 1 on long-context multi-turn tool flows; the leading
    # hypothesis is that the placeholder is OpenRouter's dummy
    # thought_signature (see Google's
    # ``thought-signatures`` doc — every functionCall part must carry
    # one, and missing signatures on prior turns ``may lead to
    # degraded model performance``). The flag exists so we can A/B test
    # without churning the API surface; default stays at ``True``
    # until the experiment proves otherwise.
    _PLACEHOLDER_LENGTH_THRESHOLD = 100

    def __init__(self, *, drop_placeholder_signature: bool = True) -> None:
        self._drop_placeholder_signature = drop_placeholder_signature

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        details = self._reasoning_details(response_message)
        summary = getattr(response_message, "reasoning_content", None) or None

        if not details and not summary:
            return None

        blocks = tuple(self._raw_to_block(raw) for raw in details)
        return StructuredReasoning(
            blocks=blocks,
            summary=_dedup_summary(blocks, summary),
            transport="openrouter",
        )

    @staticmethod
    def _reasoning_details(response_message: Any) -> list[Any]:
        """Pull ``provider_specific_fields.reasoning_details`` defensively.

        Same shape as the Anthropic-via-OpenRouter path: PSF may be None,
        a dict, or an attribute-bearing object. Any non-list ``reasoning_details``
        yields ``[]`` so callers' ``not`` checks behave.
        """
        psf = getattr(response_message, "provider_specific_fields", None)
        if psf is None:
            return []
        if isinstance(psf, dict):
            details = psf.get("reasoning_details")
        else:
            details = getattr(psf, "reasoning_details", None)
        if isinstance(details, list):
            return details
        return []

    # Keys we model as first-class fields on ``ReasoningBlock`` — everything
    # else from the raw envelope goes into ``extras`` and round-trips
    # byte-for-byte on replay. ``id`` / ``format`` / ``index`` are the ones
    # OpenRouter currently uses to bind encrypted reasoning blobs to specific
    # tool_calls; preserving them is required for thought-signature continuity
    # (2026-05-21 OTS investigation, raw-vs-pipeline A/B).
    _KNOWN_ENVELOPE_KEYS = frozenset({"type", "text", "data", "signature"})

    def _raw_to_block(self, raw: Any) -> ReasoningBlock:
        if not isinstance(raw, dict):
            raise ValueError(
                f"Gemini reasoning_details entry must be dict, got {type(raw).__name__}"
            )
        block_type = raw.get("type")
        extras = tuple(sorted((k, v) for k, v in raw.items() if k not in self._KNOWN_ENVELOPE_KEYS))
        if block_type == self._TEXT_TYPE:
            return ReasoningBlock(
                type="thinking",
                text=raw.get("text", "") or "",
                # Gemini does not emit signatures — preserve None explicitly.
                signature=raw.get("signature"),
                extras=extras,
            )
        if block_type == self._ENCRYPTED_TYPE:
            return ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=raw.get("data"),
                extras=extras,
            )
        raise ValueError(
            f"Unknown Gemini reasoning_details type: {block_type!r}; "
            f"expected {self._TEXT_TYPE!r} or {self._ENCRYPTED_TYPE!r}"
        )

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:
        if not reasoning.blocks:
            return {}
        kept = [b for b in reasoning.blocks if not self._is_placeholder_block(b)]
        if not kept:
            return {}
        return {
            "reasoning_details": [self._block_to_dict(block) for block in kept],
        }

    def _is_placeholder_block(self, block: ReasoningBlock) -> bool:
        """Detect OpenRouter's no-real-thinking placeholder.

        Returns True for ``redacted_thinking`` blocks whose payload is
        too short to be a real Gemini opaque blob, but only when the
        codec was constructed with ``drop_placeholder_signature=True``
        (the default). When the flag is off, this always returns
        False so that the placeholder round-trips back to OpenRouter
        intact. See ``_PLACEHOLDER_LENGTH_THRESHOLD`` for rationale.
        """
        if not self._drop_placeholder_signature:
            return False
        if block.type != "redacted_thinking":
            return False
        data = block.encrypted_data
        if not isinstance(data, str):
            return False
        return len(data) < self._PLACEHOLDER_LENGTH_THRESHOLD

    def _block_to_dict(self, block: ReasoningBlock) -> dict[str, Any]:
        if block.type == "thinking":
            entry: dict[str, Any] = {"type": self._TEXT_TYPE, "text": block.text}
            # Some downstream Gemini variants may begin emitting signatures —
            # carry them through if the source message provided one.
            if block.signature is not None:
                entry["signature"] = block.signature
            # Round-trip provider-specific extras (``id`` / ``format`` /
            # ``index``) so OpenRouter can bind encrypted reasoning back to
            # the corresponding ``tool_call.id`` on the next turn.
            entry.update(dict(block.extras))
            return entry
        if block.type == "redacted_thinking":
            entry = {"type": self._ENCRYPTED_TYPE}
            if block.encrypted_data is not None:
                entry["data"] = block.encrypted_data
            entry.update(dict(block.extras))
            return entry
        raise ValueError(
            f"Cannot encode block type {block.type!r} for Gemini replay — "
            f"only ``thinking`` and ``redacted_thinking`` blocks round-trip "
            "through the Gemini reasoning_details envelope."
        )


class QwenReasoningCodec:
    """Extract / replay reasoning for the Qwen family over OpenRouter.

    Qwen (like OpenAI / Grok) surfaces reasoning as an unstructured
    ``reasoning_content`` summary string — no per-block ``signature`` and,
    on the common OpenRouter route, no
    ``provider_specific_fields.reasoning_details`` envelope. The stock
    ``openai`` codec therefore *extracts* that summary fine, but its
    ``encode_for_replay`` is a no-op, so the reasoning *text* is dropped on
    the next turn and multi-turn reasoning continuity
    (:attr:`Capability.UNSIGNED_THINKING_REPLAY`) is lost — the outgoing
    assistant message carries no ``reasoning_details``.

    This codec keeps the OpenAI-style *extract* (summary → one ``thinking``
    block) but adds the unsigned-text *replay* path: on the way out it emits
    the OpenRouter ``reasoning_details`` envelope (``type="reasoning.text"``)
    carrying the turn-1 text verbatim, which OpenRouter forwards to the
    upstream Qwen route so reasoning context survives the turn boundary.

    Signed replay stays out of scope: Qwen emits no signatures, so
    :attr:`Capability.THINKING_REPLAY_ROUNDTRIP` remains unsupported — only
    the unsigned text contract is honoured here.

    When a route DOES surface structured ``reasoning_details``
    (``reasoning.text`` entries), those win over the summary, so the codec
    never fabricates a block on top of real structured data.

    Loud-fail discipline: an unknown structured block type raises
    :class:`ValueError` rather than silently dropping data.
    """

    _OPENROUTER_TEXT_TYPE = "reasoning.text"

    def extract(self, response_message: Any) -> StructuredReasoning | None:
        details = self._reasoning_details(response_message)
        summary = getattr(response_message, "reasoning_content", None) or None

        if not details and not summary:
            return None

        if details:
            blocks = tuple(self._detail_to_block(raw) for raw in details)
            return StructuredReasoning(
                blocks=blocks,
                summary=_dedup_summary(blocks, summary),
                transport="openrouter",
            )

        # Summary-only route (the common Qwen shape): normalise the
        # ``reasoning_content`` string into a single text block so the replay
        # path has verbatim text to round-trip. ``summary`` would 1:1 mirror
        # the block text, so ``_dedup_summary`` drops it.
        block = ReasoningBlock(type="thinking", text=summary or "")
        return StructuredReasoning(
            blocks=(block,),
            summary=_dedup_summary((block,), summary),
            transport="openrouter",
        )

    @staticmethod
    def _reasoning_details(response_message: Any) -> list[Any]:
        """Pull ``provider_specific_fields.reasoning_details`` defensively.

        PSF may be ``None``, a dict, or an attribute-bearing object; any
        non-list ``reasoning_details`` yields ``[]`` so callers' ``not``
        checks behave.
        """
        psf = getattr(response_message, "provider_specific_fields", None)
        if psf is None:
            return []
        if isinstance(psf, dict):
            details = psf.get("reasoning_details")
        else:
            details = getattr(psf, "reasoning_details", None)
        if isinstance(details, list):
            return details
        return []

    def _detail_to_block(self, raw: Any) -> ReasoningBlock:
        if not isinstance(raw, dict):
            raise ValueError(f"Qwen reasoning_details entry must be dict, got {type(raw).__name__}")
        block_type = raw.get("type")
        if block_type != self._OPENROUTER_TEXT_TYPE:
            raise ValueError(
                f"Unknown Qwen reasoning_details type: {block_type!r}; "
                f"expected {self._OPENROUTER_TEXT_TYPE!r}"
            )
        return ReasoningBlock(
            type="thinking",
            text=raw.get("text", "") or "",
            # Qwen does not emit signatures — preserve None explicitly.
            signature=raw.get("signature"),
        )

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:
        """Emit the OpenRouter ``reasoning_details`` text envelope for replay.

        Only text-bearing blocks round-trip; a Qwen turn without reasoning
        text yields no envelope (``{}``), matching the no-op contract of the
        other codecs when there is nothing to replay.
        """
        entries = [
            {"type": self._OPENROUTER_TEXT_TYPE, "text": block.text}
            for block in reasoning.blocks
            if block.text
        ]
        if not entries:
            return {}
        return {"reasoning_details": entries}
