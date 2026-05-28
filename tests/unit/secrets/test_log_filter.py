"""Tests for tolokaforge.secrets.log_filter."""

from __future__ import annotations

import io
import logging

import pytest

from tolokaforge.secrets import SecretManager
from tolokaforge.secrets import log_filter as log_filter_module
from tolokaforge.secrets import manager as manager_module
from tolokaforge.secrets.log_filter import (
    PLACEHOLDER,
    RedactingFilter,
    install_global_redactor,
)
from tolokaforge.secrets.manager import init_default_from

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    original = manager_module._default_manager
    manager_module._default_manager = None
    # Also reset the log-filter cache so tests start from a clean slate. The
    # cache is identity-keyed, so leaking it between tests is functionally
    # harmless, but resetting makes the cache hit/miss tests below assertable.
    saved_cached_manager = log_filter_module._cached_manager
    saved_cached_values = log_filter_module._cached_values
    log_filter_module._cached_manager = None
    log_filter_module._cached_values = frozenset()
    yield
    manager_module._default_manager = original
    log_filter_module._cached_manager = saved_cached_manager
    log_filter_module._cached_values = saved_cached_values


@pytest.fixture
def restore_log_record_factory():
    """Reset the LogRecord factory to vanilla for the test, then restore.

    Other test modules transitively import ``tolokaforge.cli.main``, which
    calls ``install_global_redactor()`` at module-import time. Without
    forcing a baseline, that leaked wrapper makes idempotency / installation
    tests order-dependent.
    """
    original = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


@pytest.fixture
def clean_root_logger(restore_log_record_factory):
    """Snapshot/restore root logger handlers + filters around each test.

    Composes with restore_log_record_factory so any factory installed during
    the test is also rolled back.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    root.handlers.clear()
    root.filters.clear()
    try:
        yield root
    finally:
        root.handlers.clear()
        root.filters.clear()
        root.handlers.extend(saved_handlers)
        root.filters.extend(saved_filters)
        root.setLevel(saved_level)


def _make_record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


# ---- Direct-filter contract -------------------------------------------------
# RedactingFilter is the explicit-attachment alternative for handlers that
# create LogRecord instances directly (bypassing the factory). The bulk of
# users go through install_global_redactor, but the filter API is still
# exercised here for completeness.


def test_filter_passthrough_when_singleton_uninitialized() -> None:
    record = _make_record("hello %s", "world")
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "hello world"


def test_filter_redacts_known_secret_value() -> None:
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    record = _make_record("calling endpoint with key=%s", "supersecretvalue123")
    assert RedactingFilter().filter(record) is True
    rendered = record.getMessage()
    assert "supersecretvalue123" not in rendered
    assert PLACEHOLDER in rendered


def test_filter_skips_short_values() -> None:
    """4-char values are below the redaction threshold and must NOT be replaced.

    Guards against over-matching short tokens like ports in unrelated prose.
    """
    init_default_from(SecretManager.from_dict({"PORT": "8080"}))
    record = _make_record("listening on port 8080")
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "listening on port 8080"


def test_filter_passes_through_non_matching_messages() -> None:
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    record = _make_record("startup complete on port %d", 50051)
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "startup complete on port 50051"


def test_filter_clears_args_after_substitution() -> None:
    """After redaction, record.args must be cleared so re-rendering is idempotent."""
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    record = _make_record("token=%s active=%s", "supersecretvalue123", "yes")
    RedactingFilter().filter(record)
    assert record.args == ()
    assert "supersecretvalue123" not in record.getMessage()
    assert PLACEHOLDER in record.getMessage()


def test_filter_redacts_secret_inside_traceback() -> None:
    """Tracebacks are a real leak vector — HTTP clients put URL+token in exceptions.

    The filter must scrub ``record.exc_text`` (the rendered traceback string)
    so the formatter does not re-render the original exc_info on the way out.
    """
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    try:
        raise RuntimeError("authorization=supersecretvalue123 failed")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    assert RedactingFilter().filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert "supersecretvalue123" not in rendered
    assert PLACEHOLDER in rendered


# ---- install_global_redactor: factory contract ------------------------------


def test_install_global_redactor_installs_a_factory(restore_log_record_factory) -> None:
    """install must replace the LogRecord factory with a wrapper."""
    original = logging.getLogRecordFactory()
    install_global_redactor()
    after = logging.getLogRecordFactory()
    assert after is not original


def test_install_global_redactor_is_idempotent(restore_log_record_factory) -> None:
    """Multiple install calls must not stack factory wrappers."""
    install_global_redactor()
    first = logging.getLogRecordFactory()
    install_global_redactor()
    install_global_redactor()
    assert logging.getLogRecordFactory() is first


def test_install_global_redactor_preserves_existing_factory(
    restore_log_record_factory,
) -> None:
    """A pre-existing custom factory must be wrapped, not replaced.

    Custom factories that mutate records (e.g., to add request IDs) must
    keep working; we only add a scrub step on top.
    """
    sentinel_marker = object()

    def custom_factory(*args, **kwargs):
        record = logging.LogRecord(*args, **kwargs)
        record.custom_marker = sentinel_marker
        return record

    logging.setLogRecordFactory(custom_factory)
    install_global_redactor()

    record = logging.getLogRecordFactory()("n", logging.INFO, "p", 1, "hi", None, None)
    assert record.custom_marker is sentinel_marker


def test_install_global_redactor_redacts_records_from_child_loggers(
    clean_root_logger,
) -> None:
    """Issue #112's actual contract: secrets emitted by ANY logger are scrubbed.

    Every module in this codebase uses ``logger = logging.getLogger(__name__)``
    and emits via that child logger; records reach root's handlers via
    propagation. The factory scrubs at record creation, so by the time any
    handler renders the record, the secret is already gone.
    """
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    buf = io.StringIO()
    clean_root_logger.addHandler(logging.StreamHandler(buf))
    clean_root_logger.setLevel(logging.INFO)
    install_global_redactor()

    child = logging.getLogger("tolokaforge.test.child_logger_redaction")
    child.info("token=%s", "supersecretvalue123")
    for h in clean_root_logger.handlers:
        h.flush()

    output = buf.getvalue()
    assert "supersecretvalue123" not in output
    assert PLACEHOLDER in output


def test_install_global_redactor_redacts_records_from_root_logger(
    clean_root_logger,
) -> None:
    """Direct emission via the root logger is also redacted."""
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    buf = io.StringIO()
    clean_root_logger.addHandler(logging.StreamHandler(buf))
    clean_root_logger.setLevel(logging.INFO)
    install_global_redactor()

    clean_root_logger.info("leak %s", "supersecretvalue123")
    for h in clean_root_logger.handlers:
        h.flush()

    output = buf.getvalue()
    assert "supersecretvalue123" not in output
    assert PLACEHOLDER in output


def test_install_global_redactor_redacts_via_lastresort_when_no_handlers(
    restore_log_record_factory, monkeypatch
) -> None:
    """The CLI host-process path: no handlers attached, records hit lastResort.

    cli/main.py installs the redactor at module-import time, before any
    code calls ``logging.basicConfig``. Records emitted later have no
    configured handlers and fall through to ``logging.lastResort`` (a
    stderr handler at WARNING+). Because we scrub at the factory, those
    records are already redacted by the time lastResort emits them.

    Uses ``propagate = False`` on the test logger to stop pytest's
    root-attached log-capture handler from intercepting; with no handlers
    found in the hierarchy, ``Logger.callHandlers`` falls through to
    ``logging.lastResort``.
    """
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    buf = io.StringIO()
    monkeypatch.setattr(logging, "lastResort", logging.StreamHandler(buf))
    install_global_redactor()

    child = logging.getLogger("tolokaforge.test.cli_host_lastresort")
    child.propagate = False
    try:
        child.warning("loaded key=%s", "supersecretvalue123")
    finally:
        child.propagate = True
    logging.lastResort.flush()

    output = buf.getvalue()
    assert output, "expected lastResort to receive a record (got empty stream)"
    assert "supersecretvalue123" not in output
    assert PLACEHOLDER in output


def test_install_global_redactor_covers_handler_added_after_install(
    clean_root_logger,
) -> None:
    """Handlers attached after install_global_redactor still see scrubbed records.

    Prior implementations attached the filter to existing handlers only;
    a deferred ``logging.basicConfig`` call (e.g. ``--verbose`` mode of
    a CLI subcommand) escaped redaction. Factory scrubbing closes that
    timing hole because the record itself is already sanitized.
    """
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    install_global_redactor()  # install BEFORE any handler exists

    buf = io.StringIO()
    clean_root_logger.addHandler(logging.StreamHandler(buf))
    clean_root_logger.setLevel(logging.INFO)

    logging.getLogger("tolokaforge.test.late_handler").error("emit %s", "supersecretvalue123")
    for h in clean_root_logger.handlers:
        h.flush()

    output = buf.getvalue()
    assert "supersecretvalue123" not in output
    assert PLACEHOLDER in output


def test_install_global_redactor_redacts_traceback_via_factory(
    clean_root_logger,
) -> None:
    """End-to-end: an exception logged via .exception() emerges scrubbed."""
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    buf = io.StringIO()
    clean_root_logger.addHandler(logging.StreamHandler(buf))
    clean_root_logger.setLevel(logging.INFO)
    install_global_redactor()

    try:
        raise RuntimeError("auth=supersecretvalue123 denied")
    except RuntimeError:
        logging.getLogger("tolokaforge.test.traceback").exception("request failed")

    for h in clean_root_logger.handlers:
        h.flush()

    output = buf.getvalue()
    assert "supersecretvalue123" not in output
    assert PLACEHOLDER in output


# ---- Cache behaviour --------------------------------------------------------


def test_known_values_cached_across_records(restore_log_record_factory) -> None:
    """The factory must call ``manager.known_values()`` once per manager,
    not once per record. Hot-path emits would otherwise re-walk every
    provider on every log line.
    """
    manager = SecretManager.from_dict({"API_KEY": "supersecretvalue123"})
    init_default_from(manager)

    call_count = {"n": 0}
    real_known_values = manager.known_values

    def counting_known_values():
        call_count["n"] += 1
        return real_known_values()

    manager.known_values = counting_known_values  # type: ignore[method-assign]

    install_global_redactor()
    factory = logging.getLogRecordFactory()
    for i in range(5):
        factory("n", logging.INFO, "p", 1, f"emit {i}", (), None)

    assert (
        call_count["n"] == 1
    ), f"known_values should be cached after first call, got {call_count['n']} calls"


def test_cache_invalidates_on_manager_swap(restore_log_record_factory) -> None:
    """Swapping the SecretManager singleton via init_default_from must
    invalidate the cache so the new manager's secrets are used immediately.
    """
    init_default_from(SecretManager.from_dict({"OLD_KEY": "oldsecretvalue"}))
    install_global_redactor()
    factory = logging.getLogRecordFactory()

    # Prime the cache with the first manager's values.
    record1 = factory("n", logging.INFO, "p", 1, "leak %s", ("oldsecretvalue",), None)
    assert PLACEHOLDER in record1.getMessage()

    # Swap manager. Identity changes; cache must refresh.
    init_default_from(SecretManager.from_dict({"NEW_KEY": "newsecretvalue"}))

    # Old secret is no longer redactable; new secret IS.
    record_old = factory("n", logging.INFO, "p", 1, "old %s", ("oldsecretvalue",), None)
    record_new = factory("n", logging.INFO, "p", 1, "new %s", ("newsecretvalue",), None)
    assert (
        "oldsecretvalue" in record_old.getMessage()
    ), "cache failed to invalidate — still scrubbing the old manager's values"
    assert "newsecretvalue" not in record_new.getMessage()
    assert PLACEHOLDER in record_new.getMessage()


# ---- _scrub_record: untested code paths -------------------------------------


def test_scrub_record_redacts_stack_info() -> None:
    """``record.stack_info`` (set by stack_info=True at log call) is also
    a leak vector when source frames contain credential literals.
    """
    init_default_from(SecretManager.from_dict({"API_KEY": "supersecretvalue123"}))
    record = _make_record("hello")
    record.stack_info = 'File "x.py", line 1, in foo\n    auth = "supersecretvalue123"\n'
    RedactingFilter().filter(record)
    assert "supersecretvalue123" not in record.stack_info
    assert PLACEHOLDER in record.stack_info


def test_factory_passthrough_when_singleton_uninitialized(
    restore_log_record_factory,
) -> None:
    """Factory must not raise or mutate when no SecretManager is configured.

    The redactor is installed early — at module-import time in some entry
    points — possibly before the SecretManager bootstrap. Records produced
    in that window must round-trip unchanged.
    """
    install_global_redactor()
    factory = logging.getLogRecordFactory()
    record = factory("n", logging.INFO, "p", 1, "hello %s", ("world",), None)
    assert record.getMessage() == "hello world"


def test_factory_passthrough_when_manager_has_no_redactable_values(
    restore_log_record_factory,
) -> None:
    """Manager exists but ``known_values()`` is empty (e.g. only short keys
    below the redaction threshold). Factory must not error and must not
    mutate the record.
    """
    init_default_from(SecretManager.from_dict({"PORT": "80", "ID": "12"}))  # both <8 chars
    install_global_redactor()
    factory = logging.getLogRecordFactory()
    record = factory("n", logging.INFO, "p", 1, "port=%d", (80,), None)
    assert record.getMessage() == "port=80"
