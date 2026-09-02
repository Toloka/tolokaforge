"""Canonization infrastructure: --update-canon flag and canon_snapshot fixture."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _pin_fake_secrets(installed_fake_secrets: dict[str, str]) -> None:
    """Pin the process SecretManager for every canonical test.

    Several modules here materialise a compose file for real, and
    materialisation injects the manager's payload into the runner service.
    Unpinned, those tests write the host's own credentials into temp compose
    files and take the empty-vs-populated branch by machine. Pinning the whole
    package rather than the modules that happen to reach it today keeps the
    next such test deterministic without anyone having to notice.
    """


def pytest_addoption(parser):
    parser.addoption(
        "--update-canon",
        action="store_true",
        default=False,
        help="Update golden canonical snapshots",
    )


@pytest.fixture
def canon_snapshot(request):
    """Fixture that compares output against golden snapshot, or updates it."""
    update_mode = request.config.getoption("--update-canon")

    class CanonSnapshot:
        def __init__(self, canon_name: str):
            self.snapshot_dir = SNAPSHOT_DIR / canon_name

        def assert_match(self, actual: dict, filename: str):
            golden_path = self.snapshot_dir / filename
            if update_mode:
                golden_path.parent.mkdir(parents=True, exist_ok=True)
                golden_path.write_text(
                    json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n"
                )
                return
            assert (
                golden_path.exists()
            ), f"Golden snapshot missing: {golden_path}. Run --update-canon"
            expected = json.loads(golden_path.read_text())
            assert actual == expected, f"Mismatch with golden {golden_path}"

    def _factory(canon_name: str) -> CanonSnapshot:
        return CanonSnapshot(canon_name)

    return _factory


# ---------------------------------------------------------------------------
# Task-specific fixtures (convenience wrappers around canonical_task_dir)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shop_orders_02_task_dir(canonical_task_dir):
    """Get path to shop_orders_02 test task."""
    return canonical_task_dir("shop_orders_02")


@pytest.fixture(scope="module")
def terminal_bench_tasks_dir(test_data_dir):
    """Get path to terminal_bench_tasks test data directory."""
    return test_data_dir / "terminal_bench_tasks"


@pytest.fixture(scope="session")
def built_wheels_dir(tmp_path_factory) -> Path:
    """Build the ``tolokaforge`` engine + every workspace-member wheel
    ``[project].dependencies`` names into one directory and return that directory.

    A scratch-venv install of the engine wheel resolves its workspace-member
    deps against this same directory (via ``uv pip install --find-links``), so
    every workspace dep the engine declares must ship a wheel here. Skips loud
    if the ``uv`` CLI is unavailable; hard-fails with captured build output on
    any build failure.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv CLI not available")

    out_dir = tmp_path_factory.mktemp("built_wheels")
    _WORKSPACE_MEMBERS: tuple[tuple[str, Path], ...] = (
        ("engine", _REPO_ROOT),
        ("models", _REPO_ROOT / "tolokaforge_models"),
        ("coding-harnesses", _REPO_ROOT / "tolokaforge_coding_harnesses"),
    )
    for label, source in _WORKSPACE_MEMBERS:
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"uv build ({label}) failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return out_dir


@pytest.fixture(scope="session")
def built_wheel(built_wheels_dir: Path) -> Path:
    """Path to the engine ``tolokaforge-*.whl`` produced by :func:`built_wheels_dir`."""
    wheels = sorted(built_wheels_dir.glob("tolokaforge-*.whl"))
    assert wheels, f"No tolokaforge wheel produced under {built_wheels_dir}"
    return wheels[-1]
