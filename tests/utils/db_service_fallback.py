"""The db-service's vendored copies of the ``tolokaforge.core.hash`` functions it calls.

The db-service image copies ``tolokaforge/env/json_db_service/`` alone, so in production
``app.py`` cannot import ``tolokaforge.core.hash`` and runs the standalone fallbacks it
defines in its ``except ImportError`` branch. A test of the shared function is a test of
the copy only if the copy is held to it, which is what this loader lets a test do.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from typing import Any

__all__ = ["load_standalone_fallback"]

_APP = "tolokaforge.env.json_db_service.app"


def load_standalone_fallback(name: str) -> Any:
    """The db-service's own definition of ``name``, with ``tolokaforge.core.hash`` blocked.

    The import is forced down the fallback branch by refusing ``tolokaforge.core.hash``
    while ``app.py`` is re-imported, and every ``json_db_service`` module is restored
    afterwards, so no other test sees the poisoned import.
    """
    real_import = builtins.__import__

    def blocking_import(module: str, *args: Any, **kwargs: Any) -> Any:
        if module == "tolokaforge.core.hash":
            raise ImportError("forced: the db-service image does not ship tolokaforge.core")
        return real_import(module, *args, **kwargs)

    saved = {
        module: sys.modules[module] for module in list(sys.modules) if "json_db_service" in module
    }
    for module in saved:
        del sys.modules[module]
    builtins.__import__ = blocking_import
    try:
        fallback = getattr(importlib.import_module(_APP), name)
        assert fallback.__module__ == _APP, (
            f"fallback poisoning failed: {name} came from {fallback.__module__}, so a parity "
            "test would compare the shared function against itself"
        )
        return fallback
    finally:
        builtins.__import__ = real_import
        for module in list(sys.modules):
            if "json_db_service" in module:
                del sys.modules[module]
        sys.modules.update(saved)
