"""The modules the offline uploader shares (the vocabulary, the profile, the model names) import
no engine module: they must load next to any engine pin, or with no engine at all."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

SHARED = (
    "tolokaforge_langfuse",
    "tolokaforge_langfuse.vocabulary",
    "tolokaforge_langfuse.profile",
    "tolokaforge_langfuse.model_names",
)

PROBE = """
import sys
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "tolokaforge" or name.startswith("tolokaforge."):
            raise ImportError(f"engine import blocked: {name}")
        return None
sys.meta_path.insert(0, _Block())
import importlib
for name in %r:
    importlib.import_module(name)
print("ok")
"""


def test_the_shared_modules_load_without_the_engine() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE % (SHARED,)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
