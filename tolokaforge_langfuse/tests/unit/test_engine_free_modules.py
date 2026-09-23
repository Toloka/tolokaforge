"""The modules the offline uploader shares (the vocabulary, the profile, the model names, the
configuration block and its reader) import no engine module: they must load next to any engine
pin, or with no engine at all."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

SHARED = (
    "tolokaforge_langfuse",
    "tolokaforge_langfuse.config",
    "tolokaforge_langfuse.vocabulary",
    "tolokaforge_langfuse.profile",
    "tolokaforge_langfuse.model_names",
    # the block reader and the plan the connector and the offline preflight run
    "tolokaforge_langfuse.preflight",
    # the converter both producers write v4 observations through: bodies in, spans out
    "tolokaforge_langfuse.otlp_spans",
    # the single-attempt transport shared by the v4 producers
    "tolokaforge_langfuse.otlp_transport",
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


# importing a module proves less than using it: the one-post exporter is built lazily, inside
# the factory, so the engine could still be reached on that path
BUILD_PROBE = PROBE.replace(
    'print("ok")',
    """
from tolokaforge_langfuse.otlp_transport import make_otlp_exporter
exporter = make_otlp_exporter("http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"},
                              retry=False)
assert type(exporter).__name__ == "SingleAttemptSpanExporter", type(exporter).__name__
assert exporter._session.get_adapter("http://127.0.0.1:9/v1/traces").max_retries.total == 0
print("ok")
""",
)


def test_the_shared_modules_load_without_the_engine() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE % (SHARED,)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# importing the reader is not using it: read a block and plan a trial with the engine blocked
READ_PROBE = PROBE.replace(
    'print("ok")',
    """
import pathlib, sys, tempfile
root = pathlib.Path(tempfile.mkdtemp())
(root / "project.yaml").write_text(
    "name: p\\nrun_defaults:\\n  observability:\\n    tracing:\\n      options:\\n"
    "        langfuse:\\n          project: p\\n          environments:\\n"
    "            test: {accepts: [trial]}\\n"
)
from tolokaforge_langfuse.preflight import load_langfuse_block, main, resolve_plan
block = load_langfuse_block(root / "project.yaml")
assert block.config.project == "p", block
assert main(["--config", str(root / "project.yaml"), "--offline", "--environment", "test",
             "--tags", "team:t,run_kind:eval,dataset:d,scope:full"]) == 0
assert not [m for m in sys.modules if m == "tolokaforge" or m.startswith("tolokaforge.")]
print("ok")
""",
)


def test_the_block_reader_and_the_offline_preflight_run_without_the_engine() -> None:
    result = subprocess.run(
        [sys.executable, "-c", READ_PROBE % (SHARED,)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_the_one_post_exporter_is_built_and_usable_without_the_engine() -> None:
    result = subprocess.run(
        [sys.executable, "-c", BUILD_PROBE % (SHARED,)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
