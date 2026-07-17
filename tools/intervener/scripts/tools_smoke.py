"""Consumer-agnostic tool smoke — proves ContextTool + AnalyzeTool run without a keyboard.

Loads a recorded trajectory into a :class:`RecordedTrialSession`, uses a
:class:`ComposedParticipant` with a :class:`RollingEventsSink` and no
controllers to drain the events, then invokes each tool programmatically.

Usage::

    uv run python tools/intervener/scripts/tools_smoke.py <trajectory.yaml>
"""

from __future__ import annotations

import sys
from pathlib import Path

from intervener import (
    ComposedParticipant,
    RollingEventsSink,
    ToolContext,
    ToolRegistry,
)

from tolokaforge.session import ParticipantRole, RecordedTrialSession


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: tools_smoke.py <trajectory.yaml>", file=sys.stderr)
        return 2

    trajectory_path = Path(argv[0])
    if not trajectory_path.is_file():
        print(f"not a file: {trajectory_path}", file=sys.stderr)
        return 2

    session = RecordedTrialSession.from_trajectory_yaml(trajectory_path)

    rolling = RollingEventsSink()
    participant = ComposedParticipant(
        participant_id="tools-smoke",
        role=ParticipantRole.OBSERVER,
        sinks=[rolling],
    )
    participant.run(session)

    registry = ToolRegistry.with_discovered()
    print(f"discovered tools: {[name for name, _ in registry.list_summary()]}\n")

    context = ToolContext(recent_events=rolling.events)

    for name in ("context", "analyze"):
        tool = registry.get(name)
        if tool is None:
            print(f"tool {name!r} not found — skipping")
            continue
        print(f"── {name} ──")
        result = tool.run("", context)
        print(result.output)
        if result.data is not None:
            print(f"  data: {result.data}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
