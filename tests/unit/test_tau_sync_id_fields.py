"""Config-driven per-table id-field resolution in :class:`TauSyncToolWrapper`.

The Tau in-memory diff-sync path emits upsert and delete ops keyed on the
resolved id_field. These tests assert both ops carry the configured key and
that a keyless record raises fail-loud.
"""

from types import SimpleNamespace

import pytest

from tests.unit.conftest import FakeMutatingDBClient
from tolokaforge.runner.id_resolution import IdFieldResolutionError
from tolokaforge.runner.tool_factory import TauSyncToolWrapper

pytestmark = pytest.mark.unit


class _FakeSyncProxy:
    """Minimal shape TauSyncToolWrapper._sync_state_changes uses."""

    def __init__(self) -> None:
        self.trial_id = "trial:0"
        self._async_proxy = SimpleNamespace(db_client=FakeMutatingDBClient())


def _make_wrapper(id_fields: dict[str, str] | None = None) -> TauSyncToolWrapper:
    # Skip ToolWrapper.__init__ (needs a full ToolSchemaModel); we only exercise
    # _sync_state_changes, which reads db_proxy and _id_fields only.
    wrapper = TauSyncToolWrapper.__new__(TauSyncToolWrapper)
    wrapper.db_proxy = _FakeSyncProxy()
    wrapper._id_fields = dict(id_fields or {})
    return wrapper


async def test_upsert_and_delete_use_configured_key():
    wrapper = _make_wrapper(id_fields={"widgets": "widget_id"})
    before = {"widgets": [{"widget_id": "W1", "status": "new"}]}
    after = {"widgets": [{"widget_id": "W1", "status": "ready"}]}

    await wrapper._sync_state_changes(before, after)

    (_table, ops) = wrapper.db_proxy._async_proxy.db_client.mutations[-1]
    upsert = next(op for op in ops if op["op"] == "upsert")
    assert upsert["key"] == "widget_id"  # config-resolved, not "id"


async def test_delete_filter_uses_configured_key():
    wrapper = _make_wrapper(id_fields={"widgets": "widget_id"})
    before = {"widgets": [{"widget_id": "W1", "status": "new"}]}
    after: dict[str, list[dict]] = {"widgets": []}

    await wrapper._sync_state_changes(before, after)

    (_table, ops) = wrapper.db_proxy._async_proxy.db_client.mutations[-1]
    delete = next(op for op in ops if op["op"] == "delete")
    assert delete["filter"] == {"widget_id": "W1"}  # not {"id": "W1"}


async def test_defaults_to_id_key_unchanged():
    wrapper = _make_wrapper(id_fields=None)  # unconfigured → "id" default
    before = {"items": [{"id": "X1", "name": "a"}]}
    after = {"items": [{"id": "X1", "name": "b"}]}

    await wrapper._sync_state_changes(before, after)

    (_table, ops) = wrapper.db_proxy._async_proxy.db_client.mutations[-1]
    upsert = next(op for op in ops if op["op"] == "upsert")
    assert upsert["key"] == "id"  # byte-for-byte compatible with pre-fix behavior


async def test_missing_key_raises_on_diff_side():
    # A record shaped for lot_id-keyed tables but no id_fields declared —
    # fail loud so the task author sees the mistake at first sync.
    wrapper = _make_wrapper(id_fields=None)
    before: dict[str, list[dict]] = {"lots": []}
    after = {"lots": [{"lot_id": "L1", "status": "open"}]}

    with pytest.raises(IdFieldResolutionError) as ei:
        await wrapper._sync_state_changes(before, after)
    assert "lots" in str(ei.value)
