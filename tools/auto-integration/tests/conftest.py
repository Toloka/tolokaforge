"""Make the tool importable in-place for tests without a full workspace install.

Adds the tool's ``src`` (so ``import auto_integration.*`` resolves) and the repo root
(so the cert drift-guard can ``import tests.canonical...`` / ``tests.integration...``)
to ``sys.path``.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"  # tools/auto-integration/src
_REPO = _HERE.parents[3]  # repo root

for _p in (_SRC, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
