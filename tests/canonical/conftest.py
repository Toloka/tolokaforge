"""Canonization infrastructure: --update-canon flag, canon_snapshot fixture, and project fixtures."""

import json
from pathlib import Path

import pytest

# Import food_delivery_2 project fixtures — only used by canonical tests
from tests.utils.project_fixtures import (  # noqa: F401
    food_delivery_2_grading_051fa6cb,
    food_delivery_2_initial_state,
    food_delivery_2_mcp_server,
    food_delivery_2_trajectory_051fa6cb,
)

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


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
            assert golden_path.exists(), (
                f"Golden snapshot missing: {golden_path}. Run --update-canon"
            )
            expected = json.loads(golden_path.read_text())
            assert actual == expected, f"Mismatch with golden {golden_path}"

    def _factory(canon_name: str) -> CanonSnapshot:
        return CanonSnapshot(canon_name)

    return _factory
