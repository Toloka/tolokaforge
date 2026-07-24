"""Run-time trial identity for log-record tagging.

The Live panel's per-trial log view filters on ``record.trial_id``. Rather
than thread an explicit ``trial_id`` through every log call site, the runner
installs :data:`TRIAL_ID_CTXVAR` for the duration of a trial's execution via
:func:`trial_id_scope`; the panel's log sink stamps the active value onto any
record that does not already carry one.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

TRIAL_ID_CTXVAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tolokaforge_trial_id", default=None
)
"""Active trial identity, shaped ``"{task_id}:{trial_index}"`` (e.g. ``"a:0"``).

``None`` outside any trial's execution scope — records emitted then carry no
``trial_id`` and never appear in a per-trial log view.
"""


@contextmanager
def trial_id_scope(trial_id: str) -> Iterator[None]:
    """Bind :data:`TRIAL_ID_CTXVAR` to ``trial_id`` for the enclosed block.

    ``trial_id`` is the ``"{task_id}:{trial_index}"`` identity. Exit restores
    the previous value via the token returned by ``set`` (PEP 567), not a plain
    ``set(None)`` — the token restores whatever was in scope on entry, so
    nested scopes unwind to their enclosing trial rather than to ``None``. The
    reset runs in a ``finally`` block so it holds even if the body raises.
    """
    token = TRIAL_ID_CTXVAR.set(trial_id)
    try:
        yield
    finally:
        TRIAL_ID_CTXVAR.reset(token)
