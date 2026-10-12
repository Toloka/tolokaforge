#!/usr/bin/env python
"""Read-only probe: where does a model put its reasoning, and do we keep it?

A model certificate's ``known_unsupported`` entry for a reasoning capability is
a *hypothesis*. This script turns it into a measurement, for roughly a cent per
model, by asking three questions the engine cannot answer from a finished run:

1. **Where does the reasoning arrive?** ``reasoning_content``, the OpenRouter
   ``reasoning`` mirror, the structured ``reasoning_details`` envelope, or
   nowhere. Providers disagree, and litellm normalises only some of them.
2. **Does the resolved codec keep it?** ``extract`` may read a field the
   provider never fills, and ``encode_for_replay`` may emit nothing — which is
   correct for OpenAI, whose routes refuse echoed reasoning, and a silent
   defect for a route that would have honoured it.
3. **Does the upstream honour the replay?** The same conversation is sent twice,
   once carrying the turn-1 reasoning and once without. A route that ignores the
   field answers identically; one that consumes it does not. Asking this matters
   because a payload on the wire is not the same as a payload that is read: on
   one measured route the echoed field changed turn-2 ``prompt_tokens`` by zero.

Writes nothing. Reads ``OPENROUTER_API_KEY`` through the engine's secret
manager, never ``os.environ`` directly.

Usage::

    uv run python scripts/analysis/probe_reasoning_transport.py
    uv run python scripts/analysis/probe_reasoning_transport.py --model x-ai/grok-4
    uv run python scripts/analysis/probe_reasoning_transport.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.presets import resolve_effective_preset
from tolokaforge.core.llm.reasoning_codec import NoReasoningCodec
from tolokaforge.core.llm.reasoning_transport import ArrivingReasoning, arriving_reasoning
from tolokaforge.secrets import get_default

#: Slugs whose presets resolve to a codec that replays nothing, plus the two
#: controls. Ordered so the controls read last in the table.
DEFAULT_SLUGS: tuple[str, ...] = (
    "x-ai/grok-4.6",
    "qwen/qwen3.8-max-0902",
    "z-ai/glm-5.3",
    "deepseek/deepseek-v4-pro-0813",
    "minimax/minimax-m3",
    "xiaomi/mimo-v2.6-pro",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "moonshotai/kimi-k2.7-code",
    "google/gemini-3.1-pro-preview",
    "openai/gpt-5.6-sol",
)

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in a Linux container.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

_SYSTEM = (
    "You are an expert software engineer working on your own inside a Linux container. "
    "Every turn, before you act, write a short note covering what the last output showed, "
    "what is left, and what you are about to run. Then make the tool call in the same turn."
)
_USER = "The service in /app is failing its tests. Investigate and fix it."
_TOOL_RESULT = (
    "total 24\n"
    "-rw-r--r-- 1 root root  812 app.py\n"
    "-rw-r--r-- 1 root root  340 test_app.py\n"
    "drwxr-xr-x 1 root root 4096 src\n"
)


@dataclass
class ProbeResult:
    """One model's answers, in the order the questions are asked."""

    slug: str
    preset: str = ""
    codec: str = ""
    upstream: str | None = None
    arrived: ArrivingReasoning = field(default_factory=ArrivingReasoning)
    content_empty: bool | None = None
    extracted: bool | None = None
    replay_emits: bool | None = None
    turn2_reasoning_without_replay: int | None = None
    turn2_reasoning_with_replay: int | None = None
    error: str | None = None

    @property
    def verdict(self) -> str:
        """What a reader should do about this row."""
        if self.error:
            return "ERROR"
        if not self.arrived.anything:
            return "no reasoning"
        if not self.arrived.readable:
            return "opaque: nothing to keep"
        if not self.extracted:
            return "FIX: arrives readable, not extracted"
        if not self.replay_emits:
            if self.honoured is False:
                return "leave: replay refused upstream"
            return "FIX: extracted, never replayed"
        return "ok"

    @property
    def honoured(self) -> bool | None:
        """Whether echoing the reasoning changed the next turn."""
        without, with_ = self.turn2_reasoning_without_replay, self.turn2_reasoning_with_replay
        if without is None or with_ is None:
            return None
        return with_ > without

    @property
    def arrives_in(self) -> str:
        """The channels column, for the table."""
        return ",".join(self.arrived.readable) or ("encrypted-only" if self.arrived.opaque else "-")


def _completion(
    slug: str, messages: list[dict[str, Any]], key: str, replay: dict[str, Any] | None = None
) -> Any:
    import litellm

    litellm.suppress_debug_info = True
    if replay:
        messages = [*messages]
        messages[-2] = {**messages[-2], **replay}
    return litellm.completion(
        model=f"openrouter/{slug}",
        messages=messages,
        tools=_TOOLS,
        temperature=0.0,
        api_key=key,
    )


def _reasoning_tokens(response: Any) -> int:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return int(getattr(details, "reasoning_tokens", 0) or 0)


#: Used only to build a replay payload for question 3, so the upstream is
#: asked the same question whatever codec the preset happens to install.
_ALWAYS_REPLAYS = OpenAISummaryReplayReasoningCodec()


def probe(slug: str, key: str) -> ProbeResult:
    """Ask all three questions of one model. Never raises."""
    out = ProbeResult(slug=slug)
    try:
        caps = build_capabilities(f"openrouter/{slug}", "openrouter")
        codec = caps.reasoning_codec
        out.codec = type(codec).__name__
        out.preset = resolve_effective_preset(f"openrouter/{slug}", "openrouter") or "-"

        base = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER},
        ]
        first = _completion(slug, base, key)
        msg = first.choices[0].message
        out.upstream = (getattr(first, "model_extra", None) or {}).get("provider")
        out.arrived = arriving_reasoning(msg)
        out.content_empty = not (getattr(msg, "content", None) or "").strip()

        reasoning = codec.extract(msg)
        out.extracted = reasoning is not None
        replay = codec.encode_for_replay(reasoning) if reasoning is not None else {}
        out.replay_emits = bool(replay)

        # Question 3 is asked with a codec that always emits, never with the
        # preset's own. Asking with the installed codec means the one case
        # worth disambiguating — it extracts and replays nothing — is the one
        # case that sends no payload, so the route is never actually asked
        # whether it would have honoured one.
        probe_replay = replay or (
            _ALWAYS_REPLAYS.encode_for_replay(reasoning) if reasoning is not None else {}
        )

        if isinstance(codec, NoReasoningCodec) or not out.arrived.anything:
            return out

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return out
        call_id = getattr(tool_calls[0], "id", "call_0")
        assistant = {
            "role": "assistant",
            "content": getattr(msg, "content", None) or "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_calls[0].function.name,
                        "arguments": tool_calls[0].function.arguments,
                    },
                }
            ],
        }
        second = [
            *base,
            assistant,
            {"role": "tool", "tool_call_id": call_id, "content": _TOOL_RESULT},
        ]
        out.turn2_reasoning_without_replay = _reasoning_tokens(_completion(slug, second, key))
        if probe_replay:
            out.turn2_reasoning_with_replay = _reasoning_tokens(
                _completion(slug, second, key, replay=probe_replay)
            )
    except Exception as exc:  # noqa: BLE001 — a probe reports failures, it does not raise them
        out.error = f"{type(exc).__name__}: {exc}"[:160]
    return out


def _render(results: list[ProbeResult]) -> str:
    head = (
        f"{'model':30s} {'preset':30s} {'codec':34s} {'upstream':14s} "
        f"{'arrives in':44s} {'ext':4s} {'rep':4s} {'t2 -/+':10s} verdict"
    )
    lines = [head, "-" * len(head)]
    for r in results:
        t2 = (
            f"{r.turn2_reasoning_without_replay}/{r.turn2_reasoning_with_replay}"
            if r.turn2_reasoning_without_replay is not None
            else "-"
        )
        lines.append(
            f"{r.slug[:30]:30s} {(r.preset or '-')[:30]:30s} {r.codec[:34]:34s} "
            f"{(r.upstream or '-')[:14]:14s} "
            f"{r.arrives_in[:44]:44s} "
            f"{('yes' if r.extracted else 'no'):4s} {('yes' if r.replay_emits else 'no'):4s} "
            f"{t2:10s} {r.verdict}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_reasoning_transport",
        description="Read-only: where a model puts its reasoning, and whether we keep it.",
    )
    parser.add_argument("--model", action="append", dest="models", help="slug; repeatable")
    parser.add_argument("--json", dest="json_path", help="also write the raw results here")
    args = parser.parse_args(argv)

    key = get_default().get_secret("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY is not set; nothing to probe.", file=sys.stderr)
        return 2

    slugs = tuple(args.models) if args.models else DEFAULT_SLUGS
    results = [probe(slug, key) for slug in slugs]
    print(_render(results))

    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump([asdict(r) | {"verdict": r.verdict} for r in results], handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
