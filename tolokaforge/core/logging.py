"""Structured logging for TolokaForge

This module provides structured logging with YAML output, context support,
and optional strict mode that raises errors on ERROR level.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class StructuredLogger:
    """Thread-safe structured logger with JSON output and strict mode

    Attributes:
        name: Logger name (typically module or trial ID)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for log output
        strict: If True, raise RuntimeError on ERROR level
        logs: Collected structured log entries
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

        # Create standard logger for console output
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)

        # Remove existing handlers to avoid duplicates
        self.logger.handlers.clear()

        # Add console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)

        # Format: timestamp - name - level - message
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger
        self.logger.propagate = False

    def _log(self, level: str, message: str, context: dict[str, Any] | None = None, **kwargs):
        """Internal logging method

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR)
            message: Log message
            context: Optional context dictionary
            **kwargs: Additional context as keyword arguments
        """
        # Get numeric log level
        log_level = getattr(logging, level)

        # Skip if below threshold (filter based on logger level)
        if log_level < self.level:
            return

        # Defensive handling for non-dict context (e.g., exception passed as positional arg)
        if context is not None and not isinstance(context, dict):
            context = {"context": str(context)}
        # Merge context and kwargs
        full_context = {**(context or {}), **kwargs}

        # Create structured log entry
        log_entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": level,
            "module": self.name,
            "message": message,
            "context": full_context,
        }
        self.logs.append(log_entry)

        # Format context for display
        if full_context:
            context_str = ", ".join(f"{k}={v}" for k, v in full_context.items())
            display_message = f"{message} ({context_str})"
        else:
            display_message = message

        self.logger.log(log_level, display_message)

        # Raise exception in strict mode for ERROR level
        if self.strict and level == "ERROR":
            error_msg = f"[STRICT MODE] {message}"
            if full_context:
                # Format context in a way that matches test expectations
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

    def save_to_file(self, path: Path):
        """Save collected logs to YAML file

        Args:
            path: Output file path
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        output = {"trial_id": self.name, "total_logs": len(self.logs), "logs": self.logs}

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
