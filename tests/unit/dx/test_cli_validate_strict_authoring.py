"""``tolokaforge validate --strict-authoring`` promotes ADAPTER_DECLARED skips to fatal.

The opt-in flag reads only the ``SkipKind`` tag every :class:`Skip` now carries:
:attr:`SkipKind.ADAPTER_DECLARED` (an adapter that is loaded and answered
:meth:`unresolvable`) is refused, :attr:`SkipKind.STRUCTURAL` (an adapter that is
uninstalled or misspelled — an environment silence) is kept never-fatal so a
pack targeting an uninstalled adapter still validates. See ADR-0042.

The four cases below sweep the two dimensions the flag partitions on — whose
silence, and whether the flag is set — over one common shape: the same minimal
pack, one row per adapter row and flag row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.adapters as adapters_package
from tolokaforge.adapters import register_adapter
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


class _AnAdapterThatImplementsNothing(BaseAdapter):
    """A plugin holding no grading overrides — inherits :class:`BaseAdapter` defaults.

    The three grading hooks each answer :meth:`unresolvable` with
    :attr:`SkipKind.ADAPTER_DECLARED` off the base defaults, so a pack targeting
    this adapter's name records an adapter-declared silence for each hook the
    gate reads.
    """


@pytest.fixture
def runner() -> CliRunner:
    """Click test runner with stderr split from stdout for the ``console`` writes."""
    return CliRunner(mix_stderr=False)


@pytest.fixture
def a_pristine_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every registration made in a test from the shared process registry.

    ``CliRunner`` invokes the CLI in-process, so ``register_adapter`` would leak
    into the real registry every other test in the session sees. The same
    two-line idiom :mod:`tests.unit.adapters.test_adapter_authoring_hooks` uses
    keeps the registration to the test that made it.
    """
    monkeypatch.setattr(adapters_package, "_ADAPTERS", {})
    monkeypatch.setattr(adapters_package, "_DISCOVERED", False)


def _write_pack(directory: Path, adapter_type: str) -> Path:
    """A minimal loadable task pack under *directory* declaring *adapter_type*.

    The grading block is empty on purpose: the tool inventory the adapter reports
    is the whole authoring surface tested here, so no rule in the block is what
    fires a skip — the inventory itself does, at :func:`_check_sections_declare_something`'s
    peer level.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "grading.yaml").write_text("{}\n")
    task_file = directory / "task.yaml"
    task_file.write_text(
        yaml.dump(
            {
                "task_id": directory.name,
                "description": "A task.",
                "adapter_type": adapter_type,
            }
        )
    )
    return task_file


def test_strict_authoring_passes_a_native_pack(runner: CliRunner, tmp_path: Path) -> None:
    """A native pack answers every hook concretely, so the flag has nothing to promote.

    :class:`~tolokaforge.adapters.native.NativeAdapter` overrides all three
    grading hooks, so every layer the gate reads is :attr:`known` — no
    :attr:`SkipKind.ADAPTER_DECLARED` skip is produced, and the flag exits ``0``
    on the same pack the default arm passes.
    """
    task_file = _write_pack(tmp_path / "native_pack", "native")

    result = runner.invoke(cli, ["validate", "--tasks", str(task_file), "--strict-authoring"])

    assert result.exit_code == 0, result.stderr
    assert "1 valid, 0 invalid" in result.stderr


def test_the_default_arm_passes_an_adapter_that_answers_unresolvable(
    runner: CliRunner, tmp_path: Path, a_pristine_registry: None
) -> None:
    """Without the flag, an adapter-declared silence is reported and never fatal.

    Baseline for the flag: the same pack test 3 refuses under
    ``--strict-authoring`` passes here, because the default arm keeps every
    :class:`Skip` non-fatal — the shipped behaviour a pack authored against an
    uninstalled adapter has always relied on.
    """
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)
    task_file = _write_pack(tmp_path / "inherits_defaults", "an_adapter_that_implements_nothing")

    result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])

    assert result.exit_code == 0, result.stderr
    assert "1 valid, 0 invalid" in result.stderr
    assert "not checked" in result.stderr


def test_strict_authoring_refuses_an_adapter_that_answers_unresolvable(
    runner: CliRunner, tmp_path: Path, a_pristine_registry: None
) -> None:
    """The flag reads :attr:`Skip.kind` and refuses the pack the adapter cannot inspect.

    ``_AnAdapterThatImplementsNothing.grading_tool_inventory`` inherits
    :meth:`BaseAdapter.grading_tool_inventory`, which answers
    :meth:`ToolInventory.unresolvable` with :attr:`SkipKind.ADAPTER_DECLARED`.
    The gate propagates that kind through to the ``grading`` skip it records
    for the tool-aware rules it cannot run, and ``--strict-authoring`` promotes
    every such skip into an invalid line — the pack's author, targeting this
    adapter in a CI they own, sees the silence as the defect it is.
    """
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)
    task_file = _write_pack(tmp_path / "inherits_defaults", "an_adapter_that_implements_nothing")

    result = runner.invoke(cli, ["validate", "--tasks", str(task_file), "--strict-authoring"])

    assert result.exit_code == 1
    assert "0 valid, 1 invalid" in result.stderr
    assert "--strict-authoring refuses" in result.stderr
    assert "adapter-declared skip" in result.stderr


def test_strict_authoring_passes_a_pack_whose_adapter_is_uninstalled(
    runner: CliRunner, tmp_path: Path, a_pristine_registry: None
) -> None:
    """A pack whose ``adapter_type`` names no installed class keeps its structural pass.

    :func:`~tolokaforge.adapters._task_loader.tool_inventory_under_adapter` and
    its siblings answer :meth:`unresolvable` with :attr:`SkipKind.STRUCTURAL`
    when :func:`adapter_class` returns ``None`` — the environment cannot even
    ask the adapter for an answer. ``--strict-authoring`` deliberately does not
    promote such skips: refusing here would break every task-pack CI shipping
    packs whose target adapter is not the CI runner's own install.
    """
    task_file = _write_pack(tmp_path / "unregistered", "an_adapter_no_one_registered")

    result = runner.invoke(cli, ["validate", "--tasks", str(task_file), "--strict-authoring"])

    assert result.exit_code == 0, result.stderr
    assert "1 valid, 0 invalid" in result.stderr
