"""Unit tests for structured logging module"""

import ast
import logging
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.logging import (
    StructuredLogger,
    clear_logger_registry,
    get_logger,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_loggers():
    """Clear logger registry before each test"""
    clear_logger_registry()
    yield
    clear_logger_registry()


def test_structured_logger_basic():
    """Test basic logger creation and usage"""
    logger = StructuredLogger("test_module")

    assert logger.name == "test_module"
    assert logger.level == logging.INFO
    assert len(logger.logs) == 0

    # Log some messages
    logger.info("Test message")
    logger.debug("Debug message")  # Won't be logged at INFO level

    assert len(logger.logs) == 1
    assert logger.logs[0]["level"] == "INFO"
    assert logger.logs[0]["message"] == "Test message"
    assert logger.logs[0]["module"] == "test_module"


def test_structured_logger_with_context():
    """Test logging with context"""
    logger = StructuredLogger("test_module")

    logger.info("Processing task", task_id="task-123", trial_index=0)

    assert len(logger.logs) == 1
    log_entry = logger.logs[0]
    assert log_entry["context"]["task_id"] == "task-123"
    assert log_entry["context"]["trial_index"] == 0


def test_logger_level_filtering():
    """Test that log level filtering works"""
    logger = StructuredLogger("test_module", level=logging.WARNING)

    logger.debug("Debug")
    logger.info("Info")
    logger.warning("Warning")
    logger.error("Error")

    # Only WARNING and ERROR should be logged
    logs = logger.get_logs()
    assert len(logs) == 2
    assert logs[0]["level"] == "WARNING"
    assert logs[1]["level"] == "ERROR"


def test_strict_mode_raises_on_error():
    """Test that strict mode raises RuntimeError on ERROR"""
    logger = StructuredLogger("test_module", strict=True)

    # Info should work fine
    logger.info("Test")
    assert len(logger.logs) == 1

    # Error should raise
    with pytest.raises(RuntimeError, match="STRICT MODE.*Failed operation"):
        logger.error("Failed operation")

    # Log should still be recorded before raising
    assert len(logger.logs) == 2
    assert logger.logs[1]["level"] == "ERROR"


def test_save_to_file(tmp_path):
    """Test saving logs to YAML file"""
    logger = StructuredLogger("test_module")

    logger.info("Message 1")
    logger.warning("Message 2", count=42)
    logger.error("Message 3")

    # Save to file
    log_file = tmp_path / "test_logs.yaml"
    logger.save_to_file(log_file)

    assert log_file.exists()

    # Load and verify
    with open(log_file) as f:
        data = yaml.safe_load(f)

    assert data["trial_id"] == "test_module"
    assert data["total_logs"] == 3
    assert len(data["logs"]) == 3
    assert data["logs"][1]["context"]["count"] == 42


def test_get_logger_singleton():
    """Test that get_logger returns same instance for same name"""
    logger1 = get_logger("module1")
    logger2 = get_logger("module1")

    assert logger1 is logger2


@pytest.mark.unit
def test_structured_logger_error_with_exception_as_context():
    """Verify structured logger doesn't crash when exception is passed as context.

    This catches the bug where orchestrator.py used printf-style:
        self.logger.error("Failed to auto-start services: %s", e)
    but StructuredLogger.error() treats the second arg as 'context' dict,
    and tries to unpack the exception as a mapping.
    """
    logger = StructuredLogger("test")
    exc = RuntimeError("test error")

    # This should NOT raise TypeError — keyword args are the correct API
    logger.error("Something failed", error=str(exc))

    # This is the actual bug pattern — passing exception as positional arg.
    # StructuredLogger.error() signature is error(message, context=None, **kwargs)
    # so the exception lands in 'context' and _log tries {**(context or {})}
    # which crashes with TypeError because exceptions aren't mappings.
    try:
        logger.error("Something failed: %s", exc)
        # If it doesn't raise, that's fine (maybe logger was fixed)
    except TypeError:
        pytest.fail(
            "StructuredLogger.error() crashed when exception passed as context. "
            "Use keyword args: logger.error('msg', error=str(e)) instead of "
            "printf-style: logger.error('msg: %s', e)"
        )


@pytest.mark.unit
def test_no_printf_style_structured_logger_calls():
    """Detect printf-style calls to StructuredLogger which cause TypeError at runtime.

    StructuredLogger methods accept (message, context=None, **kwargs).
    Printf-style calls like logger.info("msg %s", arg) pass 'arg' as 'context'
    which is semantically wrong (format string is never interpolated) and was a
    TypeError before the defensive guard was added.

    The correct pattern is: logger.info("msg", key=str(value))

    This test checks BOTH:
    - self.logger.method() calls (instance attribute)
    - logger.method() calls where logger = get_logger(...) (module-level StructuredLogger)
    """
    repo_root = Path(__file__).resolve().parents[2]
    tolokaforge_dir = repo_root / "tolokaforge"

    violations = []

    for py_file in sorted(tolokaforge_dir.rglob("*.py")):
        try:
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        # Detect if this file uses get_logger() (StructuredLogger) at module level.
        # Files with `logger = get_logger(...)` have a StructuredLogger;
        # files with `logger = logging.getLogger(...)` have a standard logger.
        uses_structured_logger = "from tolokaforge.core.logging import get_logger" in source

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("info", "error", "warning", "debug"):
                continue

            # Match two patterns:
            # 1) self.logger.method() — func.value is Attribute with attr="logger"
            # 2) logger.method() — func.value is Name with id="logger" (module-level)
            is_self_logger = isinstance(func.value, ast.Attribute) and func.value.attr == "logger"
            is_module_logger = (
                isinstance(func.value, ast.Name)
                and func.value.id == "logger"
                and uses_structured_logger
            )

            if not (is_self_logger or is_module_logger):
                continue

            caller = "self.logger" if is_self_logger else "logger"
            positional_args = node.args

            if len(positional_args) >= 3:
                rel_path = py_file.relative_to(repo_root)
                violations.append(
                    f"{rel_path}:{node.lineno}: "
                    f"{caller}.{func.attr}() has {len(positional_args)} positional args "
                    f"(max 2). Use keyword args: {caller}.{func.attr}('msg', key=val)"
                )
            elif len(positional_args) == 2:
                first_arg = positional_args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    if any(pat in first_arg.value for pat in ("%s", "%d", "%r", "%f")):
                        rel_path = py_file.relative_to(repo_root)
                        truncated = first_arg.value[:40]
                        violations.append(
                            f"{rel_path}:{node.lineno}: "
                            f'Printf-style {caller}.{func.attr}("{truncated}...", arg). '
                            f"Use keyword args instead."
                        )

    assert (
        not violations
    ), f"Found {len(violations)} printf-style StructuredLogger call(s):\n" + "\n".join(violations)
