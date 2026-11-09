"""Config-driven per-table id-field resolution in ``RunnerServiceImpl._sync_mcp_state_to_db``.

The MCP subprocess diff-sync runs on the grading critical path (before
``get_stable_hash``), so a wrong key would silently mis-hash rather than
crashing. These tests assert upsert/delete ops carry the configured key and
that missing keys fail loud.
"""

from types import SimpleNamespace

import pytest

from tests.unit.conftest import FakeMutatingDBClient
from tolokaforge.runner.db_client import InvalidOperationError, TableNotFoundError
from tolokaforge.runner.id_resolution import IdFieldResolutionError
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.state_read_limits import StateReadDeadlines

pytestmark = pytest.mark.unit


def _make_servicer(
    *,
    id_fields: dict[str, str] | None = None,
    current_state: dict[str, list[dict]] | None = None,
) -> RunnerServiceImpl:
    """Build a RunnerServiceImpl with the minimum state _sync_mcp_state_to_db reads."""
    servicer = RunnerServiceImpl.__new__(RunnerServiceImpl)
    servicer.db_client = FakeMutatingDBClient(state=current_state)
    servicer.state_read_deadlines = StateReadDeadlines()
    servicer._call_deadlines = {}
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

    ((_table, ops),) = servicer.db_client.mutations
    upsert = next(op for op in ops if op["op"] == "upsert")
    assert upsert["key"] == "lot_id"


async def test_delete_filter_uses_configured_key():
    servicer = _make_servicer(
        id_fields={"lots": "lot_id"},
        current_state={"lots": [{"lot_id": "L1", "status": "open"}]},
    )
    new_state: dict[str, list[dict]] = {"lots": []}

    await servicer._sync_mcp_state_to_db("trial:0", new_state)

    ((_table, ops),) = servicer.db_client.mutations
    delete = next(op for op in ops if op["op"] == "delete")
    assert delete["filter"] == {"lot_id": "L1"}


async def test_defaults_to_id_key_unchanged():
    servicer = _make_servicer(
        id_fields=None,
        current_state={"items": [{"id": "X1", "name": "a"}]},
    )
    new_state = {"items": [{"id": "X1", "name": "b"}]}

    await servicer._sync_mcp_state_to_db("trial:0", new_state)

    ((_table, ops),) = servicer.db_client.mutations
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


class _ServiceWithoutReplace(FakeMutatingDBClient):
    """A db-service older than the ``replace`` op."""

    def __init__(self, state: dict[str, list[dict]], refusal: Exception) -> None:
        super().__init__(state)
        self._refusal = refusal

    async def mutate(self, trial_id, table_name, operations, timeout=None, *, hashes=True):
        if operations[0]["op"] == "replace":
            raise self._refusal
        return await super().mutate(trial_id, table_name, operations, timeout, hashes=hashes)


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(InvalidOperationError("Unknown operation: replace"), id="unknown_op"),
        pytest.param(TableNotFoundError("lots"), id="table_never_created"),
    ],
)
async def test_a_service_without_replace_leaves_the_table_to_be_read_from_the_mirror(
    caplog: pytest.LogCaptureFixture, refusal: Exception
) -> None:
    """An upsert keeps the key the holder dropped; only a replace could square the table."""
    servicer = _make_servicer()
    mirror = {"lots": [{"id": "L1", "kept": 1, "dropped": 2}], "bins": [{"id": "B1"}]}
    servicer.db_client = _ServiceWithoutReplace(mirror, refusal)
    held = {"lots": [{"id": "L1", "kept": 1}], "bins": [{"id": "B1"}]}

    with caplog.at_level("WARNING"):
        synced = await servicer._sync_mcp_state_to_db("trial:0", held)

    assert synced.trusted == frozenset({"bins"})
    state = await servicer._mirror_state_after("trial:0", synced, timeout=1.0)
    assert state == {"lots": [{"id": "L1", "kept": 1, "dropped": 2}], "bins": [{"id": "B1"}]}
    (warning,) = [r.getMessage() for r in caplog.records if "refused to replace" in r.getMessage()]
    assert "'lots'" in warning and type(refusal).__name__ in warning and str(refusal) in warning


class _BudgetRecordingClient(FakeMutatingDBClient):
    """Records the budget of each request a sync sends."""

    def __init__(self, state: dict[str, list[dict]]) -> None:
        super().__init__(state)
        self.budgets: list[float] = []

    async def get_state(self, trial_id, tables=None, timeout=None, *, hashes=True):
        self.budgets.append(timeout)
        return await super().get_state(trial_id, tables, timeout, hashes=hashes)

    async def get_table_digests(self, trial_id, tables=None, timeout=None):
        self.budgets.append(timeout)
        return await super().get_table_digests(trial_id, tables, timeout)

    async def mutate(self, trial_id, table_name, operations, timeout=None, *, hashes=True):
        self.budgets.append(timeout)
        return await super().mutate(trial_id, table_name, operations, timeout, hashes=hashes)


async def test_each_request_of_a_sync_is_cut_to_what_its_call_has_left() -> None:
    """A late request of many must fail with its own cause, not the call's ceiling."""
    servicer = _make_servicer()
    client = _BudgetRecordingClient({"lots": [{"id": "L1"}], "bins": [{"id": "B1", "gone": 1}]})
    servicer.db_client = client
    held = {"lots": [{"id": "L1", "v": 2}], "bins": [{"id": "B1"}]}

    with servicer._call_budget("trial:0", 200.0):
        await servicer._sync_mcp_state_to_db("trial:0", held)

    # Digests, then a read and a mutation per table, digests, a replace, and digests.
    assert len(client.budgets) >= 7
    assert all(early > late for early, late in zip(client.budgets, client.budgets[1:]))


async def test_no_op_when_state_unchanged():
    servicer = _make_servicer(
        id_fields=None,
        current_state={"items": [{"id": "X1", "name": "a"}]},
    )
    await servicer._sync_mcp_state_to_db("trial:0", {"items": [{"id": "X1", "name": "a"}]})
    assert servicer.db_client.mutations == []
