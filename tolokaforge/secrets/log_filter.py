"""Process-global redaction of known secret values from log records.

The mechanism is a wrapped :func:`logging.setLogRecordFactory` that scrubs
each :class:`logging.LogRecord` at creation time — before any logger,
filter, handler, or formatter sees it. This is the only mechanism robust
to all three failure modes that defeat naïve approaches:

1. Logger-level filters never fire for records emitted by child loggers;
   :meth:`Logger.callHandlers` walks ancestors' handlers but not their
   filters, so a filter on the root logger is invisible to
   ``logging.getLogger(__name__).info(...)``.
2. Handler-level filters only cover handlers attached at install time;
   handlers added later (e.g. by a deferred ``logging.basicConfig`` call
   in a CLI subcommand) bypass them.
3. The :data:`logging.lastResort` fallback handler — used when no
   handlers are configured anywhere in the logger hierarchy — is not
   reachable from ``root.handlers`` at all.

Scrubbing at the record factory sidesteps all three: the record itself is
already redacted by the time *any* handler runs, regardless of when or
where it was attached.

Caveats — this is a *substring* matcher; it does NOT catch encoded forms
(URL-encoded inside a URL, base64'd in a header dump, JSON-escaped).
Engineers must still avoid logging secret-bearing objects directly.
"""

from __future__ import annotations

import logging
from typing import Any

from tolokaforge.secrets.manager import SecretManager, compose_escaped, get_default_or_none

PLACEHOLDER = "***REDACTED***"

# Cache the last seen SecretManager instance and its known_values() snapshot.
# `known_values()` walks every provider and serializes every key — wasteful
# to repeat per log record. The cache is invalidated by identity, so
# `init_default_from(...)` swapping the singleton is picked up automatically.
_cached_manager: SecretManager | None = None
_cached_values: frozenset[str] = frozenset()


def _current_redaction_values() -> frozenset[str]:
    """Return the active scrub set, refreshing the cache on manager swap."""
    global _cached_manager, _cached_values
    manager = get_default_or_none()
    if manager is None:
        return frozenset()
    if manager is not _cached_manager:
        _cached_manager = manager
        resolved = manager.known_values()
        # A credential also travels in its compose-escaped form, written into a
        # materialised compose file. That form is not the value the manager
        # holds, so without it a compose error quoting the file back scrubs to
        # nothing. Escaping a value with no ``$`` is the identity, so the union
        # only grows for the values that need it.
        _cached_values = resolved | {compose_escaped(value) for value in resolved}
    return _cached_values


def _scrub_record(record: logging.LogRecord) -> None:
    """Replace every known secret value in a record with :data:`PLACEHOLDER`.

    Touches ``record.msg`` + ``record.args`` (the rendered message),
    ``record.exc_text`` (pre-rendered traceback — populated here so the
    formatter uses the scrubbed text directly instead of re-rendering
    from ``exc_info``), and ``record.stack_info``. No-op while the
    SecretManager singleton is uninitialized or has no redactable values.
    """
    values = _current_redaction_values()
    if not values:
        return

    rendered = record.getMessage()
    scrubbed = rendered
    for value in values:
        if value in scrubbed:
            scrubbed = scrubbed.replace(value, PLACEHOLDER)
    if scrubbed != rendered:
        record.msg = scrubbed
        record.args = ()

    if record.exc_info or record.exc_text:
        if not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        for value in values:
            if value in record.exc_text:
                record.exc_text = record.exc_text.replace(value, PLACEHOLDER)
        # Clearing exc_info prevents Formatter.format() from re-rendering and
        # un-doing the scrub.
        record.exc_info = None

    if record.stack_info:
        for value in values:
            if value in record.stack_info:
                record.stack_info = record.stack_info.replace(value, PLACEHOLDER)


class RedactingFilter(logging.Filter):
    """A :class:`logging.Filter` wrapper around :func:`_scrub_record`.

    Most callers should use :func:`install_global_redactor` — the factory
    install covers every record produced in the process. This class
    remains as an explicit attachment hook for the rare handler that
    creates :class:`LogRecord` instances directly (bypassing
    :meth:`Logger.makeRecord` and therefore the factory).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


# Marker attribute we set on our factory wrapper so a second
# install_global_redactor() call recognises its own work and does not
# wrap recursively.
_FACTORY_SENTINEL = "_tolokaforge_redactor_factory"


def install_global_redactor() -> None:
    """Install a process-global :class:`LogRecord` factory that redacts secrets.

    Idempotent: a second call is a no-op (detected via a sentinel
    attribute on the installed factory wrapper). Composes with any
    pre-existing custom factory — the previous factory is wrapped, not
    replaced, so its mutations are preserved and then scrubbed.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, _FACTORY_SENTINEL, False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        _scrub_record(record)
        return record

    setattr(redacting_factory, _FACTORY_SENTINEL, True)
    logging.setLogRecordFactory(redacting_factory)
