"""End-to-end tests for the ``tolokaforge adapter convert`` command.

Exercise the full command with a stub conversion adapter and assert it writes
native task bundles and invokes the shared-resources hook. Also guards that the
command's imports (notably ``tolokaforge.adapters.bundle_writer``) resolve, so a
missing module surfaces as a test failure rather than a runtime crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.adapters as adapters_pkg
from tolokaforge.adapters.base import BaseAdapter, NativeTaskBundle
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


class _StubConversionAdapter(NativeAdapter):
    """Minimal conversion-capable adapter.

    Subclasses the concrete NativeAdapter (so all abstract methods are
    satisfied) and overrides only what ``convert`` needs.
    """

    def __init__(self, params: dict | None = None):
        # NativeAdapter.__init__ requires a tasks_glob; we override discovery
        # anyway, so any placeholder is fine.
        super().__init__({"tasks_glob": "unused/**", **(params or {})})
        self.shared_calls: list = []

    def get_task_ids(self) -> list[str]:
        return ["t1", "t2"]

    def convert_to_native(self, task_id: str) -> NativeTaskBundle:
        return NativeTaskBundle(
            task_config={"name": f"Task {task_id}", "category": "tool_use"},
            grading_config={"combine": {"method": "weighted", "pass_threshold": 1.0}},
            fixtures={"tools": [{"name": "noop"}]},
            metadata={"source_adapter": "stub"},
        )

    def write_shared_resources(self, output_dir: Path, bundle: NativeTaskBundle) -> None:
        # A real shared-resources writer owns creating its target directory
        # (e.g. the old _domain/ writer did mkdir(parents=True)).
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.shared_calls.append(out)
        (out / "_shared_marker").write_text("ok", encoding="utf-8")


class _NoSharedAdapter(_StubConversionAdapter):
    """Conversion adapter that does NOT emit shared resources (default no-op)."""

    write_shared_resources = BaseAdapter.write_shared_resources


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def test_convert_writes_native_bundles(runner, tmp_path, monkeypatch):
    stub = _StubConversionAdapter()
    # convert() does `from tolokaforge.adapters import get_adapter` at call time,
    # so patching the module attribute is sufficient.
    monkeypatch.setattr(adapters_pkg, "get_adapter", lambda name, params: stub)

    out = tmp_path / "out"
    result = runner.invoke(
        cli,
        ["adapter", "convert", "--name", "stub", "--tasks-glob", "x/**", "--output", str(out)],
    )

    assert result.exit_code == 0, result.output
    for tid in ("t1", "t2"):
        assert (out / tid / "task.yaml").exists()
        assert (out / tid / "grading.yaml").exists()
        assert (out / tid / "fixtures" / "tools.json").exists()
        assert yaml.safe_load((out / tid / "task.yaml").read_text())["task_id"] == tid

    # shared-resources hook runs exactly once (with the first bundle)
    assert len(stub.shared_calls) == 1
    assert (out / "_shared_marker").exists()


def test_convert_validate_exits_nonzero_for_invalid_output(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(adapters_pkg, "get_adapter", lambda name, params: _StubConversionAdapter())

    result = runner.invoke(
        cli,
        [
            "adapter",
            "convert",
            "--name",
            "stub",
            "--tasks-glob",
            "x/**",
            "--output",
            str(tmp_path / "out"),
            "--validate",
        ],
    )

    assert result.exit_code == 1, result.stderr
    assert "Validation: 0 valid, 2 invalid" in result.stderr


def test_convert_works_without_shared_resources(runner, tmp_path, monkeypatch):
    stub = _NoSharedAdapter()
    monkeypatch.setattr(adapters_pkg, "get_adapter", lambda name, params: stub)

    out = tmp_path / "out"
    result = runner.invoke(
        cli,
        ["adapter", "convert", "--name", "stub", "--tasks-glob", "x/**", "--output", str(out)],
    )

    assert result.exit_code == 0, result.output
    assert (out / "t1" / "task.yaml").exists()
    # default hook is a no-op → no shared artifacts emitted
    assert not (out / "_shared_marker").exists()
