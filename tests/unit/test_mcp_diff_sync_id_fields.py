"""Config-driven per-table id-field resolution in ``RunnerServiceImpl._sync_mcp_state_to_db``.

The MCP subprocess diff-sync runs on the grading critical path (before
``get_stable_hash``), so a wrong key would silently mis-hash rather than
crashing. These tests assert upsert/delete ops carry the configured key and
that missing keys fail loud.
"""

from types import SimpleNamespace

import pytest

from tests.unit.conftest import FakeMutatingDBClient
from tolokaforge.runner.id_resolution import IdFieldResolutionError
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit


def _make_servicer(
    *,
    id_fields: dict[str, str] | None = None,
    current_state: dict[str, list[dict]] | None = None,
) -> RunnerServiceImpl:
    """Build a RunnerServiceImpl with the minimum state _sync_mcp_state_to_db reads."""
    servicer = RunnerServiceImpl.__new__(RunnerServiceImpl)
    servicer.db_client = FakeMutatingDBClient(state=current_state)
    trial = SimpleNamespace(
        task_description=SimpleNamespace(
            grading=SimpleNamespace(state_checks=SimpleNamespace(id_fields=dict(id_fields or {})))
        )
    )
    servicer.trials = {"trial:0": trial}
    return servicer


async def test_upsert_uses_configured_key():
    servicer = _make_servicer(
        id_fields={"lots": "lot_id"},
        current_state={"lots": [{"lot_id": "L1", "status": "open"}]},
    )
    new_state = {"lots": [{"lot_id": "L1", "status": "released"}]}

    await servicer._sync_mcp_state_to_db("trial:0", new_state)

    _table, ops = servicer.db_client.mutations[-1]
    upsert = next(op for op in ops if op["op"] == "upsert")
    assert upsert["key"] == "lot_id"


async def test_delete_filter_uses_configured_key():
    servicer = _make_servicer(
        id_fields={"lots": "lot_id"},
        current_state={"lots": [{"lot_id": "L1", "status": "open"}]},
    )
    new_state: dict[str, list[dict]] = {"lots": []}

    await servicer._sync_mcp_state_to_db("trial:0", new_state)

    _table, ops = servicer.db_client.mutations[-1]
    delete = next(op for op in ops if op["op"] == "delete")
    assert delete["filter"] == {"lot_id": "L1"}


async def test_defaults_to_id_key_unchanged():
    servicer = _make_servicer(
        id_fields=None,
        current_state={"items": [{"id": "X1", "name": "a"}]},
    )
    new_state = {"items": [{"id": "X1", "name": "b"}]}

    await servicer._sync_mcp_state_to_db("trial:0", new_state)

    _table, ops = servicer.db_client.mutations[-1]
    upsert = next(op for op in ops if op["op"] == "upsert")
    assert upsert["key"] == "id"


async def test_missing_key_raises_on_diff_side():
    # id_fields undeclared but records keyed by lot_id → fail loud rather than
    # collapsing every record to a single ``None`` bucket.
    servicer = _make_servicer(id_fields=None, current_state={"lots": []})
    new_state = {"lots": [{"lot_id": "L1"}, {"lot_id": "L2"}]}

    with pytest.raises(IdFieldResolutionError) as ei:
        await servicer._sync_mcp_state_to_db("trial:0", new_state)
    assert "lots" in str(ei.value)


async def test_no_op_when_state_unchanged():
    servicer = _make_servicer(
        id_fields=None,
        current_state={"items": [{"id": "X1", "name": "a"}]},
    )
    await servicer._sync_mcp_state_to_db("trial:0", {"items": [{"id": "X1", "name": "a"}]})
    assert servicer.db_client.mutations == []
