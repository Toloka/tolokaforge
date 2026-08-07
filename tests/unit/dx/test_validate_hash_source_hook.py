"""What ``tolokaforge validate`` says about a hash block whose source an adapter supplies.

The gate is static — it constructs no adapters and runs against packs whose adapter
package is not installed — so it asks the registered *class* what lies beneath the
authored ``state_checks.hash`` block. The adapter modelled here reads the shape the
frozen-core family really uses: a golden-actions fixture under the task directory that
the authored block never names.

The point of the four rows is that the same pack, byte for byte, is passed, refused or
reported purely on what the adapter answers — and that an adapter this environment has
never heard of still answers nothing, which is the compatibility promise #940 makes.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import tolokaforge.adapters as adapters_package
from tolokaforge.adapters import available_adapters, register_adapter
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.grading.config_validation import (
    AdapterHashSource,
    HashSourceLayer,
    SuppliedSourceState,
)
from tolokaforge.core.models import TaskConfig
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit

_AN_ADAPTER_TYPE = "an_adapter_reading_its_own_fixture"
_WHERE = "fixtures/golden_actions.json"


class _AnAdapterReadingItsOwnFixture(BaseAdapter):
    """Answers the hash-source question off the task directory, as the real family does.

    Abstract on purpose: the hook is a classmethod so the static gate can ask it
    without ever constructing an adapter, and this class proves that path by being
    unconstructible.
    """

    @classmethod
    def grading_hash_source_layer(cls, task: TaskConfig, task_dir: Path) -> HashSourceLayer:
        golden = task_dir / _WHERE
        if not golden.exists():
            return HashSourceLayer(
                supplied=AdapterHashSource(where=_WHERE, state=SuppliedSourceState.MISSING)
            )
        replayed = json.loads(golden.read_text())
        return HashSourceLayer(
            supplied=AdapterHashSource(
                where=_WHERE,
                state=SuppliedSourceState.USABLE if replayed else SuppliedSourceState.EMPTY,
            )
        )


@pytest.fixture
def a_registered_adapter() -> Iterator[None]:
    """Register the fake for the duration of one test, and take it out again.

    Discovery is forced first so the registration is not the thing that triggers it,
    and the key is popped rather than the registry replaced, so nothing else a test
    session registered is disturbed.
    """
    available_adapters()
    register_adapter(_AN_ADAPTER_TYPE, _AnAdapterReadingItsOwnFixture)
    yield
    adapters_package._ADAPTERS.pop(_AN_ADAPTER_TYPE, None)


def _write_pack(root: Path, adapter_type: str, golden_actions: list | None) -> Path:
    """One frozen-shaped pack: the hash enabled, no source declared, and a fixture.

    *golden_actions* is what the fixture holds, or ``None`` for no fixture at all —
    the three states the adapter reports, written as the only thing that differs.
    """
    task_yaml = textwrap.dedent(f"""
        task_id: a_pack_whose_adapter_supplies_the_hash_source
        name: "Adapter-supplied hash source"
        category: test
        description: "A frozen-shaped pack whose hash source lives in a fixture."
        adapter_type: {adapter_type}
        initial_state:
          json_db: null
        tools:
          agent:
            enabled: []
          user:
            enabled: []
        user_simulator:
          mode: "scripted"
          scripted_flow:
            - role: "user"
              content: "hi"
        grading: "grading.yaml"
        """).strip()
    grading_yaml = textwrap.dedent("""
        combine:
          method: weighted
          weights:
            state_checks: 1.0
        state_checks:
          hash:
            enabled: true
            weight: 1.0
        """).strip()

    root.mkdir(parents=True, exist_ok=True)
    (root / "task.yaml").write_text(task_yaml)
    (root / "grading.yaml").write_text(grading_yaml)
    if golden_actions is not None:
        fixtures = root / "fixtures"
        fixtures.mkdir(exist_ok=True)
        (fixtures / "golden_actions.json").write_text(json.dumps(golden_actions))
    return root / "task.yaml"


def _validate(task_file: Path) -> tuple[int, str]:
    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])
    return result.exit_code, result.stderr


def test_a_usable_supplied_source_passes_the_pack_and_reports_nothing(
    tmp_path: Path, a_registered_adapter: None
) -> None:
    """Checked, not merely unrefused: the bare block draws no finding and no skip.

    The skip is the whole difference from the uninstalled-adapter row below. Printing
    one here would tell the author their healthy pack was not checked, when in fact
    the adapter answered and the gate agreed.
    """
    task_file = _write_pack(tmp_path / "pack", _AN_ADAPTER_TYPE, [{"name": "add_note"}])

    exit_code, out = _validate(task_file)

    assert "1 valid, 0 invalid" in out
    assert "state_checks.hash" not in out
    assert exit_code == 0


def test_a_supplied_source_that_is_gone_refuses_the_pack_naming_the_fixture(
    tmp_path: Path, a_registered_adapter: None
) -> None:
    """The blind spot: the same pack, refused because the fixture it grades by is gone.

    The refusal has to name the fixture — the author's only way to the fix is the path
    the adapter reads, which the authored block never mentions.
    """
    task_file = _write_pack(tmp_path / "pack", _AN_ADAPTER_TYPE, None)

    exit_code, out = _validate(task_file)

    assert "0 valid, 1 invalid" in out
    assert _WHERE in out
    assert "missing" in out
    assert exit_code != 0


def test_a_supplied_source_that_replays_nothing_refuses_the_pack_as_empty(
    tmp_path: Path, a_registered_adapter: None
) -> None:
    """An empty fixture compares against as little as no fixture, and is named as such.

    Distinct from the missing row because the fix is: the file is where the author
    expects it, and reading "missing" for a file that is plainly there would send them
    looking for the wrong problem.
    """
    task_file = _write_pack(tmp_path / "pack", _AN_ADAPTER_TYPE, [])

    exit_code, out = _validate(task_file)

    assert "0 valid, 1 invalid" in out
    assert _WHERE in out
    assert "empty" in out
    assert exit_code != 0


def test_the_same_pack_under_an_uninstalled_adapter_is_reported_rather_than_refused(
    tmp_path: Path, a_registered_adapter: None
) -> None:
    """Criterion 3: an adapter this environment has no class for still answers nothing.

    Deliberately run under the same fixture that registers the fake, and against an
    adapter type it never registered: together with the unregistered-by-omission lock
    in ``test_validate_grading_migrations.py`` this proves both that an uninstalled
    adapter skips and that the registration does not leak into packs naming something
    else. The sentence is pinned verbatim — an author reading it must be told the
    question was unanswerable, not that their pack is fine.
    """
    task_file = _write_pack(tmp_path / "pack", "an_adapter_nothing_installed", None)

    exit_code, out = _validate(task_file)

    assert "1 valid, 0 invalid" in out
    assert "state_checks.hash.enabled not checked" in out
    assert "an external adapter may compute the source" in out
    assert exit_code == 0
