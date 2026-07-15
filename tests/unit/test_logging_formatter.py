"""Unit tests for `StructuredFormatter` and `configure_root_logging`."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from typing import Any

import pytest

from tolokaforge.core.logging import (
    _LOG_RECORD_RESERVED,
    _TOLOKAFORGE_ROOT_HANDLER_SENTINEL,
    LogFormat,
    StructuredFormatter,
    configure_root_logging,
    silence_root_logging,
)

pytestmark = pytest.mark.unit


FIXED_INSTANT = datetime(2026, 7, 14, 14, 30, 0, 500_000)


def _fixed_clock() -> datetime:
    return FIXED_INSTANT


class _FakeStream(io.StringIO):
    """StringIO with a controllable `isatty()` result."""

    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:  # noqa: D401 — simple accessor
        return self._is_tty


def _make_record(
    *,
    level: int = logging.INFO,
    name: str = "tolokaforge.orch",
    message: str = "trial started",
    extras: dict[str, Any] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="tolokaforge/core/orchestrator.py",
        lineno=42,
        msg=message,
        args=None,
        exc_info=None,
    )
    if extras:
        for key, value in extras.items():
            record.__dict__[key] = value
    return record


@pytest.fixture
def isolated_root() -> None:
    """Snapshot root handlers/level; restore after the test."""

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Plain / pretty / JSON rendering with fixed clock
# ---------------------------------------------------------------------------


def test_plain_bare_record_double_space_middle():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(message="started")

    line = formatter.format(record)

    assert line == "14:30:00.500 | INFO |  | started"


def test_plain_scoped_record_sorted_keys():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(
        extras={"run_id": "abc", "judge": "kimi-k2", "sample": 42},
    )

    line = formatter.format(record)

    assert line == "14:30:00.500 | INFO | judge=kimi-k2 run_id=abc sample=42 | trial started"


def test_json_scoped_record_parses_and_matches_schema():
    formatter = StructuredFormatter(LogFormat.JSON, clock=_fixed_clock)
    record = _make_record(
        extras={"judge": "kimi-k2", "run_id": "abc", "sample": 42},
    )

    line = formatter.format(record)
    payload = json.loads(line)

    assert payload == {
        "ts": "14:30:00.500",
        "level": "INFO",
        "logger": "tolokaforge.orch",
        "message": "trial started",
        "extra": {"judge": "kimi-k2", "run_id": "abc", "sample": 42},
    }


def test_json_bare_record_has_empty_extra_dict():
    formatter = StructuredFormatter(LogFormat.JSON, clock=_fixed_clock)
    record = _make_record(message="started")

    payload = json.loads(formatter.format(record))

    assert payload["extra"] == {}


def test_pretty_info_wraps_line_with_cyan_and_reset():
    formatter = StructuredFormatter(LogFormat.PRETTY, clock=_fixed_clock)
    record = _make_record(message="started")

    line = formatter.format(record)

    assert line.startswith("\x1b[36m14:30:00.500")
    assert line.endswith("started\x1b[0m")


@pytest.mark.parametrize(
    ("level", "level_name", "escape"),
    [
        (logging.DEBUG, "DEBUG", "\x1b[2m"),
        (logging.INFO, "INFO", "\x1b[36m"),
        (logging.WARNING, "WARNING", "\x1b[33m"),
        (logging.ERROR, "ERROR", "\x1b[1;31m"),
    ],
)
def test_pretty_level_palette(level: int, level_name: str, escape: str):
    formatter = StructuredFormatter(LogFormat.PRETTY, clock=_fixed_clock)
    record = _make_record(level=level, message="m")

    line = formatter.format(record)

    assert line.startswith(escape)
    assert f"| {level_name} |" in line
    assert line.endswith("\x1b[0m")


def test_pretty_ansi_codes_match_display_theme():
    """The hardcoded ANSI table must stay in lockstep with `_display.THEME`.

    Rich renders a `THEME` style on a truecolor terminal as exactly the
    bytes we emit — this guards against a THEME palette change silently
    diverging from the formatter.
    """
    from rich.console import Console

    from tolokaforge.cli._display import THEME
    from tolokaforge.core.logging import _ANSI_RESET, _LEVEL_ANSI

    console = Console(force_terminal=True, theme=THEME, color_system="truecolor")
    for level_name, theme_style in (
        ("DEBUG", "muted"),
        ("INFO", "info"),
        ("WARNING", "warn"),
        ("ERROR", "error"),
    ):
        with console.capture() as cap:
            console.print("x", style=theme_style, end="")
        rendered = cap.get()
        expected = f"{_LEVEL_ANSI[level_name]}x{_ANSI_RESET}"
        assert rendered == expected, f"ANSI drift for {level_name!r}: THEME={rendered!r}"


# ---------------------------------------------------------------------------
# Scope filtering rules
# ---------------------------------------------------------------------------


def test_reserved_set_covers_bare_logrecord_dict():
    """The reserved set must include every attribute a blank `LogRecord`
    populates plus the two `Formatter.format`-injected keys."""
    bare = logging.LogRecord(
        name="n",
        level=logging.INFO,
        pathname="p",
        lineno=1,
        msg="m",
        args=None,
        exc_info=None,
    )
    populated = set(bare.__dict__) | {"message", "asctime"}
    missing = populated - _LOG_RECORD_RESERVED
    assert not missing, f"reserved set missing keys populated by stdlib: {sorted(missing)}"


def test_reserved_attributes_never_leak_into_scope_pairs():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(extras={"payload": "keep"})
    # Overwrite every reserved key with a distinguishable value; the formatter
    # must not render any of these as `k=v` pairs. `msg` and `args` are what
    # `LogRecord.getMessage()` interpolates — keep the record valid by
    # pinning `msg` to the marker string (still a reserved key we want to
    # verify is filtered) and `args` to `None` (no interpolation).
    marker = "LEAKED"
    skip_render_keys = {"args"}
    for key in _LOG_RECORD_RESERVED - skip_render_keys:
        record.__dict__[key] = marker
    record.__dict__["args"] = None

    line = formatter.format(record)

    # `levelname` and `msg` are reserved slots the formatter itself reads —
    # overwriting them puts the marker in the level and message segments,
    # which is fine. What matters: no reserved key leaks into the *scope*
    # segment as a `k=v` pair.
    scope_segment = line.split(" | ")[2]
    assert marker not in scope_segment
    assert scope_segment == "payload=keep"


def test_underscore_prefixed_extras_are_dropped():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(extras={"_internal": "hidden", "shown": "yes"})

    line = formatter.format(record)

    assert "_internal" not in line
    assert "shown=yes" in line


def test_values_with_whitespace_or_pipe_render_via_repr():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(
        extras={"spaced": "hello world", "piped": "pipe|here", "clean": "ok"},
    )

    line = formatter.format(record)

    assert "spaced='hello world'" in line
    assert "piped='pipe|here'" in line
    assert "clean=ok" in line


def test_scope_pairs_sorted_alphabetically():
    formatter = StructuredFormatter(LogFormat.PLAIN, clock=_fixed_clock)
    record = _make_record(extras={"zebra": 1, "alpha": 2, "mike": 3})

    line = formatter.format(record)

    scope_segment = line.split(" | ")[2]
    assert scope_segment == "alpha=2 mike=3 zebra=1"


def test_json_non_jsonable_value_falls_back_to_repr():
    formatter = StructuredFormatter(LogFormat.JSON, clock=_fixed_clock)

    class Weird:
        def __repr__(self) -> str:
            return "<weird!>"

    record = _make_record(extras={"obj": Weird()})

    payload = json.loads(formatter.format(record))

    assert payload["extra"] == {"obj": "<weird!>"}


# ---------------------------------------------------------------------------
# configure_root_logging: idempotence and isatty auto-selection
# ---------------------------------------------------------------------------


def _tolokaforge_root_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False)
    ]


def test_configure_root_logging_non_tty_stream_auto_selects_plain(isolated_root):
    stream = _FakeStream(is_tty=False)

    configure_root_logging(level=logging.DEBUG, log_format=None, stream=stream)

    handlers = _tolokaforge_root_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, StructuredFormatter)
    assert handlers[0].formatter.mode is LogFormat.PLAIN
    assert logging.getLogger().level == logging.DEBUG


def test_configure_root_logging_tty_stream_auto_selects_pretty(isolated_root):
    stream = _FakeStream(is_tty=True)

    configure_root_logging(log_format=None, stream=stream)

    handlers = _tolokaforge_root_handlers()
    assert len(handlers) == 1
    assert handlers[0].formatter.mode is LogFormat.PRETTY


def test_configure_root_logging_is_idempotent(isolated_root):
    stream = _FakeStream(is_tty=False)

    configure_root_logging(stream=stream)
    configure_root_logging(stream=stream)

    assert len(_tolokaforge_root_handlers()) == 1


def test_configure_root_logging_leaves_foreign_handlers_alone(isolated_root):
    foreign = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(foreign)

    configure_root_logging(stream=_FakeStream(is_tty=False))
    configure_root_logging(stream=_FakeStream(is_tty=False))

    root_handlers = logging.getLogger().handlers
    assert foreign in root_handlers
    assert len(_tolokaforge_root_handlers()) == 1


def test_configure_root_logging_explicit_format_overrides_auto(isolated_root):
    stream = _FakeStream(is_tty=True)  # would auto-select PRETTY

    configure_root_logging(log_format=LogFormat.JSON, stream=stream)

    handlers = _tolokaforge_root_handlers()
    assert handlers[0].formatter.mode is LogFormat.JSON


def test_configure_root_logging_replaces_previous_formatter(isolated_root):
    """Second call with a different format installs a fresh handler."""
    stream = _FakeStream(is_tty=False)

    configure_root_logging(log_format=LogFormat.PLAIN, stream=stream)
    first = _tolokaforge_root_handlers()[0]

    configure_root_logging(log_format=LogFormat.JSON, stream=stream)
    handlers = _tolokaforge_root_handlers()

    assert len(handlers) == 1
    assert handlers[0] is not first
    assert handlers[0].formatter.mode is LogFormat.JSON


def test_redaction_scrubs_secret_before_formatter_renders(isolated_root):
    """The record factory scrubs before StructuredFormatter renders.

    Locks the composed pipeline (redactor factory → StructuredFormatter),
    not just each piece independently. A future refactor that swaps their
    order would leak the secret into the formatted line and fail here.
    """
    from tolokaforge.secrets import log_filter as log_filter_module
    from tolokaforge.secrets import manager as manager_module
    from tolokaforge.secrets.log_filter import (
        _FACTORY_SENTINEL,
        PLACEHOLDER,
        install_global_redactor,
    )
    from tolokaforge.secrets.manager import SecretManager, init_default_from

    secret = "supersecretvalue123"
    saved_factory = logging.getLogRecordFactory()
    saved_manager = manager_module._default_manager
    saved_cached_manager = log_filter_module._cached_manager
    saved_cached_values = log_filter_module._cached_values
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        manager_module._default_manager = None
        log_filter_module._cached_manager = None
        log_filter_module._cached_values = frozenset()

        init_default_from(SecretManager.from_dict({"API_KEY": secret}))
        install_global_redactor()
        assert getattr(logging.getLogRecordFactory(), _FACTORY_SENTINEL, False)

        stream = _FakeStream(is_tty=False)
        configure_root_logging(log_format=LogFormat.PLAIN, stream=stream)
        logging.getLogger("t").warning("token %s in header", secret)

        output = stream.getvalue()
        assert secret not in output, output
        assert PLACEHOLDER in output, output
    finally:
        logging.setLogRecordFactory(saved_factory)
        manager_module._default_manager = saved_manager
        log_filter_module._cached_manager = saved_cached_manager
        log_filter_module._cached_values = saved_cached_values


# ---------------------------------------------------------------------------
# silence_root_logging (B2 --display=none plumbing)
# ---------------------------------------------------------------------------


def test_silence_root_logging_bumps_handler_above_critical(isolated_root):
    """After configure_root_logging + silence_root_logging, the tolokaforge
    sentinel handler is at CRITICAL+1 — even CRITICAL records are filtered."""
    configure_root_logging(log_format=LogFormat.PLAIN, stream=_FakeStream(is_tty=False))
    silence_root_logging()

    handler = next(
        h
        for h in logging.getLogger().handlers
        if getattr(h, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False)
    )
    assert handler.level == logging.CRITICAL + 1


def test_silence_root_logging_actually_silences_records(isolated_root):
    """End-to-end: with silencing applied, an emitted record produces no bytes
    on the sentinel handler's stream. Locks the observable behaviour, not just
    the handler.level attribute."""
    stream = _FakeStream(is_tty=False)
    configure_root_logging(log_format=LogFormat.PLAIN, stream=stream)
    silence_root_logging()

    logging.getLogger("tolokaforge.probe").critical("should be swallowed")

    assert stream.getvalue() == ""


def test_silence_root_logging_no_op_before_configure(isolated_root):
    """Called before configure_root_logging: no sentinel handler installed,
    so no exception raised and no side effects."""
    # No configure call — bare root state (fixture cleared handlers).
    silence_root_logging()  # Must not raise.

    tolokaforge_handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False)
    ]
    assert tolokaforge_handlers == []


def test_silence_root_logging_leaves_foreign_handlers_alone(isolated_root):
    """A foreign handler (e.g. pytest caplog) is NOT bumped."""
    foreign = logging.Handler()
    foreign.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        configure_root_logging(log_format=LogFormat.PLAIN, stream=_FakeStream(is_tty=False))
        silence_root_logging()

        assert foreign.level == logging.INFO
    finally:
        root.removeHandler(foreign)


def test_silence_root_logging_is_idempotent(isolated_root):
    configure_root_logging(log_format=LogFormat.PLAIN, stream=_FakeStream(is_tty=False))
    silence_root_logging()
    silence_root_logging()

    handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False)
    ]
    assert len(handlers) == 1
    assert handlers[0].level == logging.CRITICAL + 1
