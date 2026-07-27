"""Adapter surface the runner advertises.

The runner advertises the built-in native adapter; it does not perform adapter
translation. External adapters are host-side entry-point plugins outside the
runner's advertised surface, so this list is sourced from a runner-owned
constant rather than the adapters package — the runner never reaches
``tolokaforge.adapters``.
"""

from tolokaforge.runner.models import AdapterType

BUILTIN_ADAPTERS: tuple[str, ...] = (AdapterType.NATIVE.value,)
