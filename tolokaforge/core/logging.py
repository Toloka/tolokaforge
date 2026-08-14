"""Structured logging for TolokaForge

This module provides structured logging with YAML output, context support,
and optional strict mode that raises errors on ERROR level.

It also provides the process-wide log formatter (`StructuredFormatter`,
`LogFormat`) and root-handler bootstrap (`configure_root_logging`) used
by the CLI to render every `logging.getLogger(...)` record with a
consistent `HH:MM:SS.mmm | LEVEL | k=v | message` shape on stderr.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

import yaml

from tolokaforge.core.redaction import (
    REDACTED_PLACEHOLDER,
    NoRedaction,
    RedactionPolicy,
    key_is_sensitive,
)


class LogFormat(str, Enum):
    """Line shape emitted by `StructuredFormatter`."""

    PRETTY = "pretty"
    PLAIN = "plain"
    JSON = "json"


_LOG_RECORD_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_ANSI_RESET = "\x1b[0m"
_LEVEL_ANSI: dict[str, str] = {
    "DEBUG": "\x1b[2m",
    "INFO": "\x1b[36m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[1;31m",
    "CRITICAL": "\x1b[1;31m",
}

_TOLOKAFORGE_ROOT_HANDLER_SENTINEL = "_tolokaforge_root_handler"


def _render_scalar(key: str, value: Any) -> str:
    text = str(value)
    if any(ch.isspace() for ch in text) or "|" in text:
        return f"{key}={value!r}"
    return f"{key}={text}"


def _to_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


class StructuredFormatter(logging.Formatter):
    """Render a `LogRecord` in one of the three `LogFormat` modes.

    Layout (pretty & plain):

        HH:MM:SS.mmm | LEVEL | k1=v1 k2=v2 | message

    - Timestamp is millisecond-resolution local time. `clock` may inject a
      deterministic `datetime` factory for tests (default: `datetime.now`).
    - Scope pairs are every `LogRecord.__dict__` key not in
      `_LOG_RECORD_RESERVED` and not starting with `_`. Keys are sorted
      alphabetically for deterministic goldens.
    - Values render bare (`k=v`) unless `str(v)` contains whitespace or
      `|`, in which case the value renders via `repr` (`k='hello world'`).
    - Empty scope renders as an empty middle segment — the two spaces
      between the surrounding `|` delimiters are preserved (`... | LEVEL
      |  | message`).

    JSON mode emits one JSON object per line with keys
    `{"ts", "level", "logger", "message", "extra"}`. Non-JSONable
    extra values fall back to `repr`.

    Pretty mode wraps the entire line with the ANSI escape sequence for
    the record's level (INFO=cyan, WARNING=yellow, ERROR=bold red,
    DEBUG=dim) — matching the semantics of
    `tolokaforge.dx._display.THEME`.
    """

    def __init__(
        self,
        mode: LogFormat,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self.mode = mode
        self._clock = clock or datetime.now

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self._render_timestamp()
        scope = self._scope_pairs(record)
        message = record.getMessage()
        if self.mode is LogFormat.JSON:
            return self._render_json(timestamp, record, scope, message)
        scope_segment = " ".join(_render_scalar(k, v) for k, v in scope.items())
        line = f"{timestamp} | {record.levelname} | {scope_segment} | {message}"
        if self.mode is LogFormat.PRETTY:
            color = _LEVEL_ANSI.get(record.levelname)
            if color is not None:
                line = f"{color}{line}{_ANSI_RESET}"
        return line

    def _render_timestamp(self) -> str:
        now = self._clock()
        return f"{now:%H:%M:%S}.{now.microsecond // 1000:03d}"

    @staticmethod
    def _scope_pairs(record: logging.LogRecord) -> dict[str, Any]:
        return {
            key: record.__dict__[key]
            for key in sorted(record.__dict__)
            if key not in _LOG_RECORD_RESERVED and not key.startswith("_")
        }

    @staticmethod
    def _render_json(
        timestamp: str,
        record: logging.LogRecord,
        scope: dict[str, Any],
        message: str,
    ) -> str:
        payload = {
            "ts": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "extra": {k: _to_jsonable(v) for k, v in scope.items()},
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_root_logging(
    *,
    level: int = logging.INFO,
    log_format: LogFormat | None = None,
    stream: TextIO | None = None,
) -> None:
    """Install (or replace) the tolokaforge root log handler.

    Idempotent: a previously installed tolokaforge root handler (tagged
    via the `_tolokaforge_root_handler` sentinel attribute) is removed
    before the new one is added. Foreign handlers on `logging.root`
    (e.g. pytest's `caplog`) are left untouched.

    Both the root logger and the tolokaforge handler are set to `level`
    — the handler level is authoritative so a child logger that raises
    its own effective level (e.g. `Orchestrator.__init__` calling
    `logging.getLogger("tolokaforge.docker").setLevel(INFO)`) still gets
    filtered when root `-q` requests WARNING+ on console.

    `log_format=None` auto-selects `PRETTY` if `stream.isatty()` returns
    truthy, otherwise `PLAIN`. `stream=None` defaults to `sys.stderr`.
    """
    target_stream = stream if stream is not None else sys.stderr
    if log_format is None:
        log_format = LogFormat.PRETTY if target_stream.isatty() else LogFormat.PLAIN

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(StructuredFormatter(log_format))
    handler.setLevel(level)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root.addHandler(handler)
    root.setLevel(level)


def silence_root_logging() -> None:
    """Raise the tolokaforge root log handler above ``CRITICAL``.

    Locates the handler tagged with :data:`_TOLOKAFORGE_ROOT_HANDLER_SENTINEL`
    and sets ``handler.level = logging.CRITICAL + 1`` so no log record
    emits through it. Foreign handlers (e.g. pytest's ``caplog``) and any
    embedder-installed handlers are left untouched — silencing is
    handler-local by design.

    No-op when :func:`configure_root_logging` has not been called yet
    (the sentinel handler is absent). Idempotent.
    """
    for handler in logging.getLogger().handlers:
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            handler.setLevel(logging.CRITICAL + 1)


class StructuredLogger:
    """Thread-safe structured logger with in-memory list + YAML output.

    Attributes:
        name: Logger name (typically module or trial ID)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for log output
        strict: If True, raise RuntimeError on ERROR level
        logs: Collected structured log entries

    Records are routed through the stdlib logger with `propagate=True` so
    the root handler installed by `configure_root_logging` owns rendering.
    The in-memory `logs` list and `save_to_file` YAML shape are separate
    from the console rendering.
    """

    def __init__(
        self,
        name: str,
        level: int = logging.INFO,
        log_file: Path | None = None,
        strict: bool = False,
    ):
        self.name = name
        self.level = level
        self.log_file = log_file
        self.strict = strict
        self.logs: list[dict[str, Any]] = []

        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.handlers.clear()
        self.logger.propagate = True

    @staticmethod
    def _sanitize_extra(context: dict[str, Any]) -> dict[str, Any]:
        """Rename LogRecord-reserved keys and redact sensitive values.

        Two guarantees:

        1. Keys colliding with `LogRecord` attributes (`module`, `name`, …)
           are prefixed with `ctx_` so `logging.Logger.log(..., extra=...)`
           doesn't raise `KeyError` at `LogRecord.__init__`. The
           `StructuredFormatter` reads renamed keys via the same rule.
        2. Values under keys that name a credential are replaced with the
           placeholder. The vocabulary and the matching rule live in
           :mod:`tolokaforge.core.redaction`, shared with the artifact
           writer; the match runs on the sanitized (post-rename) key.
           `_sanitize_extra` is the single choke point on the log path —
           callers cannot bypass it.
        """
        sanitized: dict[str, Any] = {}
        for key, value in context.items():
            safe_key = f"ctx_{key}" if key in _LOG_RECORD_RESERVED else key
            if key_is_sensitive(safe_key):
                sanitized[safe_key] = REDACTED_PLACEHOLDER
            else:
                sanitized[safe_key] = value
        return sanitized

    def _log(self, level: str, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Internal logging method

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR)
            message: Log message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments
        """
        log_level = getattr(logging, level)

        if log_level < self.level:
            return

        # Defensive handling for non-dict context (e.g., exception passed as positional arg)
        if context is not None and not isinstance(context, dict):
            context = {"context": str(context)}
        raw_context = {**(context or {}), **kwargs}
        # Sanitize once and use the redacted view everywhere the values
        # are surfaced: the in-memory log list, the stdlib LogRecord's
        # ``extra=`` payload, and the strict-mode RuntimeError message.
        # Any bypass reintroduces the CodeQL clear-text-logging surface.
        full_context = self._sanitize_extra(raw_context)

        log_entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": level,
            "module": self.name,
            "message": message,
            "context": full_context,
        }
        self.logs.append(log_entry)

        self.logger.log(log_level, message, extra=full_context)

        if self.strict and level == "ERROR":
            error_msg = f"[STRICT MODE] {message}"
            if full_context:
                context_parts = [f"{k}={v}" for k, v in full_context.items()]
                error_msg += f" ({', '.join(context_parts)})"
            raise RuntimeError(error_msg)

    def debug(self, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Log debug message

        Args:
            message: Debug message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments
        """
        self._log("DEBUG", message, context, **kwargs)

    def info(self, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Log info message

        Args:
            message: Info message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments
        """
        self._log("INFO", message, context, **kwargs)

    def warning(self, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Log warning message

        Args:
            message: Warning message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments
        """
        self._log("WARNING", message, context, **kwargs)

    def error(self, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Log error message (raises in strict mode)

        Args:
            message: Error message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments

        Raises:
            RuntimeError: If strict mode is enabled
        """
        self._log("ERROR", message, context, **kwargs)

    def save_to_file(self, path: Path, redaction: RedactionPolicy = NoRedaction()):
        """Save collected logs to YAML file

        Args:
            path: Output file path
            redaction: Applied to each record on its way out; the default writes
                them as collected. A trial bundle passes its writer's policy,
                which is what reaches a credential nested inside a record's
                context — `_sanitize_extra` reads top-level keys only. The
                collected records themselves are left untouched.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        records = [redaction.redact_mapping(entry) for entry in self.logs]
        output = {"trial_id": self.name, "total_logs": len(records), "logs": records}

        with open(path, "w") as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def get_logs(self) -> list[dict[str, Any]]:
        """Get all collected logs

        Returns:
            List of log entries
        """
        return self.logs.copy()

    def clear_logs(self):
        """Clear all collected logs"""
        self.logs.clear()


# Global logger registry
_loggers: dict[str, StructuredLogger] = {}


def get_logger(name: str, level: int = logging.INFO, strict: bool = False) -> StructuredLogger:
    """Get or create a logger instance

    Args:
        name: Logger name
        level: Logging level (default: INFO)
        strict: If True, raise on ERROR (default: False)

    Returns:
        StructuredLogger instance
    """
    # Create unique key for logger with its config
    logger_key = f"{name}:{level}:{strict}"

    if logger_key not in _loggers:
        _loggers[logger_key] = StructuredLogger(name, level, strict=strict)

    return _loggers[logger_key]


def init_trial_logger(
    trial_id: str, verbose: bool = False, strict: bool = False
) -> StructuredLogger:
    """Initialize logger for a trial

    Args:
        trial_id: Trial identifier (e.g., "task-123:0")
        verbose: If True, use DEBUG level (default: False)
        strict: If True, raise on ERROR (default: False)

    Returns:
        StructuredLogger instance configured for trial
    """
    level = logging.DEBUG if verbose else logging.INFO
    return get_logger(trial_id, level=level, strict=strict)


def clear_logger_registry():
    """Clear all cached loggers (useful for testing)"""
    global _loggers
    _loggers.clear()
