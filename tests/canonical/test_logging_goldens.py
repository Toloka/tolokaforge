"""Byte-level canonical goldens for `StructuredFormatter`.

Every ``.log`` golden under ``tests/canonical/golden/logging/`` locks the
exact bytes a fixed-clock ``StructuredFormatter`` produces for a single
``logging.LogRecord``. The suite covers the three modes
(``pretty`` / ``plain`` / ``json``) with and without extras, the four
level palette entries in ``pretty`` mode, and the ``ctx_``-prefix rename
that ``StructuredLogger._sanitize_extra`` performs when an extra key
collides with a reserved ``LogRecord`` attribute.

**Golden regeneration.** Bytes are locked byte-for-byte. When the palette
in ``tolokaforge.cli._display.THEME`` shifts or the layout changes, the
goldens must be regenerated in the *same* commit as the code change:

    uv run pytest tests/canonical/test_logging_goldens.py --update-canon

Never edit the ``.log`` files by hand — text editors normalise ANSI escape
codes and CR/LF, which drifts the bytes away from what the formatter emits.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.logging import (
    LogFormat,
    StructuredFormatter,
    StructuredLogger,
)

pytestmark = pytest.mark.canonical


GOLDEN_DIR = Path(__file__).parent / "golden" / "logging"

FIXED_INSTANT = datetime(2026, 7, 14, 14, 30, 0, 500_000)

SCOPED_EXTRAS: dict[str, Any] = {"judge": "kimi-k2", "run_id": "abc", "sample": 42}


def _fixed_clock() -> datetime:
    return FIXED_INSTANT


def _make_record(
    *,
    level: int,
    extras: dict[str, Any] | None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tolokaforge.orch",
        level=level,
        pathname="tolokaforge/core/orchestrator.py",
        lineno=42,
        msg="trial started",
        args=None,
        exc_info=None,
    )
    for key, value in (extras or {}).items():
        record.__dict__[key] = value
    return record


def _formatter(mode: LogFormat) -> StructuredFormatter:
    return StructuredFormatter(mode, clock=_fixed_clock)


# ---------------------------------------------------------------------------
# Case table — one row per golden file. `extras_factory` runs lazily so the
# reserved-collision case can exercise `StructuredLogger._sanitize_extra`.
# ---------------------------------------------------------------------------

_CASES: tuple[tuple[str, LogFormat, int, Any], ...] = (
    ("pretty__bare.log", LogFormat.PRETTY, logging.INFO, None),
    ("pretty__scoped.log", LogFormat.PRETTY, logging.INFO, SCOPED_EXTRAS),
    ("plain__bare.log", LogFormat.PLAIN, logging.INFO, None),
    ("plain__scoped.log", LogFormat.PLAIN, logging.INFO, SCOPED_EXTRAS),
    ("json__bare.log", LogFormat.JSON, logging.INFO, None),
    ("json__scoped.log", LogFormat.JSON, logging.INFO, SCOPED_EXTRAS),
    ("pretty__warning.log", LogFormat.PRETTY, logging.WARNING, None),
    ("pretty__error.log", LogFormat.PRETTY, logging.ERROR, None),
    ("pretty__debug.log", LogFormat.PRETTY, logging.DEBUG, None),
    (
        "pretty__reserved_collision.log",
        LogFormat.PRETTY,
        logging.INFO,
        StructuredLogger._sanitize_extra({"module": "shadowed"}),
    ),
)


def _render_case(mode: LogFormat, level: int, extras: dict[str, Any] | None) -> bytes:
    record = _make_record(level=level, extras=extras)
    return (_formatter(mode).format(record) + "\n").encode("utf-8")


@pytest.mark.parametrize(
    ("golden_name", "mode", "level", "extras"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_golden_bytes_match(
    request: pytest.FixtureRequest,
    golden_name: str,
    mode: LogFormat,
    level: int,
    extras: dict[str, Any] | None,
) -> None:
    """The formatter's output must match the golden's bytes byte-for-byte."""

    actual = _render_case(mode, level, extras)
    golden_path = GOLDEN_DIR / golden_name

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_bytes(actual)
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_logging_goldens.py --update-canon`."
    )
    expected = golden_path.read_bytes()
    if actual != expected:
        pytest.fail(
            f"Golden byte drift for {golden_name}:\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )


# ---------------------------------------------------------------------------
# JSON-parseability: the on-wire schema is a compatibility surface.
# ---------------------------------------------------------------------------

_JSON_GOLDEN_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "json__bare.log": {
        "ts": "14:30:00.500",
        "level": "INFO",
        "logger": "tolokaforge.orch",
        "message": "trial started",
        "extra": {},
    },
    "json__scoped.log": {
        "ts": "14:30:00.500",
        "level": "INFO",
        "logger": "tolokaforge.orch",
        "message": "trial started",
        "extra": {"judge": "kimi-k2", "run_id": "abc", "sample": 42},
    },
}


@pytest.mark.parametrize(
    "golden_name", sorted(_JSON_GOLDEN_EXPECTATIONS), ids=sorted(_JSON_GOLDEN_EXPECTATIONS)
)
def test_json_goldens_parse_to_schema(golden_name: str) -> None:
    """Every JSON golden line must round-trip through ``json.loads`` and
    match the ``{ts, level, logger, message, extra}`` schema exactly."""

    golden_path = GOLDEN_DIR / golden_name
    raw = golden_path.read_bytes().rstrip(b"\n").decode("utf-8")
    payload = json.loads(raw)
    assert payload == _JSON_GOLDEN_EXPECTATIONS[golden_name]
    assert set(payload) == {"ts", "level", "logger", "message", "extra"}
