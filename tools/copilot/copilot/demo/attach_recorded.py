"""``copilot-demo`` — replay a recorded trajectory into either participant.

Reads a captured ``trajectory.yaml`` (optionally truncated at a given turn),
attaches the requested participant to a :class:`RecordedTrialSession`, drains
the event stream, and writes the resulting session log next to the trajectory.

Both participant types produce identical session-log shape — the contract is
genuinely shared. See ``docs/OPEN_AGENT_LOOP.md`` §3 and the M2 milestone
description.

    uv run copilot-demo --trajectory trajectory.yaml --as copilot
    uv run copilot-demo --archive <run-dir> --trial MAN-34 --truncate-turn 3 --as human
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from rich.console import Console

from copilot.participants import (
    HumanCLIParticipant,
    LLMCopilotParticipant,
    Participant,
)
from tolokaforge.session import RecordedTrialSession

_CHOICES = ("copilot", "human")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Attach a participant to a recorded trajectory.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--trajectory",
        type=Path,
        help="Path to a captured trajectory.yaml file.",
    )
    src.add_argument(
        "--archive",
        type=Path,
        help="Path to a run directory containing trials/<task_id>/<trial_index>/trajectory.yaml.",
    )
    parser.add_argument(
        "--trial", type=str, help="Task ID inside --archive (required with --archive)."
    )
    parser.add_argument(
        "--trial-index",
        type=int,
        default=0,
        help="Trial index inside --archive/<trial>/ (default: 0).",
    )
    parser.add_argument(
        "--truncate-turn",
        type=int,
        default=None,
        help="Stop synthesis after N completed agent turns. Omit for full replay.",
    )
    parser.add_argument("--as", dest="participant", choices=_CHOICES, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path for the session log YAML (default: alongside the trajectory).",
    )
    parser.add_argument(
        "--auto-inject",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="LLM participant only: submit high-urgency suggestions as InjectMessage (rejected by recorded session).",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=None,
        help="Human participant only: file of one intervention per line (for non-interactive demos).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trajectory_path = _resolve_trajectory_path(args)
    session = RecordedTrialSession.from_trajectory_yaml(
        trajectory_path, truncate_at_turn=args.truncate_turn
    )

    participant = _build_participant(args)
    log = participant.run(session)

    out_path = args.out or trajectory_path.with_name(f"session_log__{args.participant}.yaml")
    with out_path.open("w") as f:
        yaml.safe_dump(
            {
                "trial_id": session.trial_id,
                "participant_id": participant.participant_id,
                "participant_role": participant.role.value,
                "entries": log.to_yaml_dict(),
                "captured_interventions": [
                    intervention.model_dump(mode="json")
                    for intervention in session.captured_interventions
                ],
            },
            f,
            sort_keys=False,
        )

    console = Console(stderr=True)
    console.print(
        f"[green]session log written[/green] {out_path}  "
        f"({len(log.entries)} entries, "
        f"{len(session.captured_interventions)} interventions submitted)"
    )
    return 0


def _resolve_trajectory_path(args: argparse.Namespace) -> Path:
    if args.trajectory is not None:
        return args.trajectory
    if args.trial is None:
        raise SystemExit("--archive requires --trial <task_id>")
    return args.archive / "trials" / args.trial / str(args.trial_index) / "trajectory.yaml"


def _build_participant(args: argparse.Namespace) -> Participant:
    if args.participant == "copilot":
        return LLMCopilotParticipant(auto_inject=args.auto_inject)
    script: list[str] | None = None
    if args.script is not None:
        script = [line.rstrip("\n") for line in args.script.read_text().splitlines()]
    return HumanCLIParticipant(non_interactive_script=script)


if __name__ == "__main__":
    sys.exit(main())
