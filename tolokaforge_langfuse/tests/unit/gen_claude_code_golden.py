"""Regenerate the transcript projection golden.

Run it only when a change to the projection is intended, and read the diff:

    uv run python tolokaforge_langfuse/tests/unit/gen_claude_code_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from test_transcripts import CLEAN, GOLDEN, bodies, built  # noqa: E402  (test-local helpers)

from tolokaforge_langfuse import transcripts as tr


def main() -> None:
    GOLDEN.write_text(
        json.dumps(bodies(built(tr.redact(tr.read_claude_output(CLEAN)))), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {GOLDEN.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
