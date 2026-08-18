"""Model data and per-model policy subclasses for tolokaforge.

This wheel ships the bundled ``pricing.json``, ``model_presets.yaml``,
and ``providers.yaml`` tables, plus the per-model policy subclasses
and the ``ModelCertificate`` registry that
the engine consumes through its light seam in
:mod:`tolokaforge.core.model_data`.

The wheel imports no first-party engine module at load time; a repo
that has never installed :mod:`tolokaforge` can still ``import
tolokaforge_models`` and read the three constants below.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "__api_version__",
    "__version__",
    "minimum_engine_version",
]

__version__: Final[str] = "1.2.0"
"""PEP 440 version of the models wheel itself."""

__api_version__: Final[int] = 1
"""Integer version of the loader contract between this wheel and the
engine. Bumped whenever the engine-side loader must change to keep
reading this wheel's registrations."""

minimum_engine_version: Final[str] = ">=0.17,<1.0"
"""PEP 440 specifier naming the engine range this wheel targets. The
engine consults this string at import time to refuse to boot against
an incompatible pair (see :mod:`tolokaforge.core.model_data`)."""
