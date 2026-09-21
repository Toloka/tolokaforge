"""Pin the in-process observer seam and its persisted shapes (ADR-0011 / ADR-0047)."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models import Message, ToolCall, TracingConfig, Trajectory
from tolokaforge.observability.factory import (
    RunIdentityDocument,
    write_tracing_receipt,
)
from tolokaforge.observability.observer import (
    CompositeTrialObserver,
    ExportReceipt,
    InMemoryTrialObserver,
    ModelRef,
    NullTrialObserver,
    TrialIdentity,
    TrialObserver,
    TrialObserverCallLog,
    safely,
)
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.canonical

HOOKS = (
    "trial_started",
    "generation",
    "tool_call",
    "trial_finished",
    "trial_persisted",
    "run_finished",
)
IDENTITY = TrialIdentity(run_id="run-1", task_id="T-1", trial_index=0, attempt_id=2)
AT = datetime(2026, 9, 18, tzinfo=timezone.utc)


def _calls():
    return [
        ("trial_started", {"models": {"agent": ModelRef("test", "agent")}, "started_at": AT}),
        (
            "generation",
            {
                "role": "agent",
                "index": 1,
                "turn": 0,
                "request": [Message(role="user", content="hello", ts=AT)],
                "result": GenerationResult("hi"),
                "started_at": AT,
                "ended_at": AT,
            },
        ),
        (
            "tool_call",
            {
                "role": "agent",
                "index": 2,
                "call": ToolCall(id="call-1", name="lookup", arguments={}),
                "result": ToolResult(success=True, output="found"),
                "started_at": AT,
                "ended_at": AT,
            },
        ),
        (
            "trial_finished",
            {
                "trajectory": Trajectory(
                    task_id="T-1", trial_index=0, start_ts=AT, end_ts=AT, messages=[]
                ),
                "error": None,
            },
        ),
        ("trial_persisted", {"trial_dir": Path("trials/T-1/0")}),
    ]


def test_hook_surface_and_argument_shapes(canon_snapshot):
    public = {name for name in vars(TrialObserver) if not name.startswith("_")}
    assert public == set(HOOKS)
    surface = {}
    for name in HOOKS:
        signature = inspect.signature(getattr(TrialObserver, name))
        assert inspect.signature(getattr(InMemoryTrialObserver, name)) == signature
        surface[name] = str(signature)
    canon_snapshot("trial_observer_contract").assert_match(surface, "hooks.json")


@pytest.mark.parametrize(
    "observer", [NullTrialObserver(), InMemoryTrialObserver(), CompositeTrialObserver()]
)
def test_implementations_accept_the_protocol_calls(observer):
    assert isinstance(observer, TrialObserver)
    for name, kwargs in _calls():
        assert getattr(observer, name)(IDENTITY, **kwargs) is None
    assert isinstance(observer.run_finished(), ExportReceipt)
    assert not isinstance(object(), TrialObserver)


def test_fixture_records_all_arguments_and_order():
    observer = InMemoryTrialObserver(receipt=ExportReceipt(exporter="memory", spans_exported=1))
    assert observer.call_log == TrialObserverCallLog()
    expected = []
    for name, kwargs in _calls():
        getattr(observer, name)(IDENTITY, **kwargs)
        expected.append((name, {"identity": IDENTITY, **kwargs}))
    assert observer.run_finished() is observer.receipt
    assert observer.call_log.calls == [*expected, ("run_finished", {})]
    assert InMemoryTrialObserver().call_log == TrialObserverCallLog()


@pytest.mark.parametrize("hook", HOOKS)
def test_safely_isolates_every_hook_failure_and_logs_it(hook, caplog):
    observer = InMemoryTrialObserver(fail_on={hook: RuntimeError("receiver unavailable")})
    kwargs = dict(_calls()).get(hook, {})
    args = () if hook == "run_finished" else (IDENTITY,)
    assert safely(getattr(observer, hook), *args, **kwargs) is None
    assert observer.call_log.calls == [(hook, {"identity": IDENTITY, **kwargs} if args else {})]
    assert "receiver unavailable" in caplog.text


def test_safely_returns_success_and_preserves_process_interrupts():
    value = object()
    assert safely(lambda: value) is value

    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        safely(interrupt)


def test_composite_fans_out_all_hooks_after_a_failure():
    broken = InMemoryTrialObserver(fail_on={name: RuntimeError(name) for name in HOOKS})
    first, second = InMemoryTrialObserver(), InMemoryTrialObserver()
    composite = CompositeTrialObserver((first, broken, second))
    for name, kwargs in _calls():
        getattr(composite, name)(IDENTITY, **kwargs)
    receipt = composite.run_finished()
    assert first.call_log.calls == second.call_log.calls == broken.call_log.calls
    assert [name for name, _ in second.call_log.calls] == list(HOOKS)
    assert receipt.export_failures == 1 and receipt.flushed is False


def test_composite_merges_unknown_counters_and_preserves_receiver_details():
    receipts = [
        ExportReceipt(
            exporter="one",
            spans_queued=4,
            spans_exported=3,
            spans_dropped=1,
            export_failures=1,
            extra={"custom.accepted": 3},
            details=({"exporter": "one", "project": "a"},),
        ),
        ExportReceipt(
            exporter="two",
            spans_queued=2,
            spans_exported=2,
            flushed=False,
            extra={"custom.accepted": 2, "another.received": 2},
            details=({"exporter": "two", "project": "b"},),
        ),
    ]
    composite = CompositeTrialObserver([InMemoryTrialObserver(receipt=r) for r in receipts])
    assert composite.run_finished() == ExportReceipt(
        exporter="one, two",
        spans_queued=6,
        spans_exported=5,
        spans_dropped=1,
        export_failures=1,
        flushed=False,
        extra={"custom.accepted": 5, "another.received": 2},
        details=({"exporter": "one", "project": "a"}, {"exporter": "two", "project": "b"}),
    )
    assert ExportReceipt.merge([]) == ExportReceipt()


def test_receipt_wire_shape_and_persistence(canon_snapshot, tmp_path):
    receipt = ExportReceipt(
        spans_queued=5,
        spans_exported=3,
        spans_dropped=2,
        export_failures=1,
        flushed=False,
        exporter="example",
        extra={"example.accepted": 3},
        details=({"exporter": "example", "destination": "test", "optional": None},),
    )
    canon_snapshot("trial_observer_contract").assert_match(
        {"default": ExportReceipt().model_dump_json(), "populated": receipt.model_dump_json()},
        "receipt.json",
    )
    assert ExportReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    path = write_tracing_receipt(tmp_path, receipt)
    assert ExportReceipt.model_validate_json(path.read_text()) == receipt
    assert json.loads(path.read_text()) == receipt.model_dump(mode="json")


def test_run_identity_wire_shape(canon_snapshot):
    document = RunIdentityDocument(
        run_id="run-1",
        run_tag="v2",
        written_at=AT.isoformat(),
        engine_version="1.2.3",
    )
    canon_snapshot("trial_observer_contract").assert_match(
        {"wire": document.model_dump_json()},
        "run_identity.json",
    )
    assert RunIdentityDocument.model_validate_json(document.model_dump_json()) == document


@pytest.mark.parametrize(
    "model,fields",
    [
        (ExportReceipt, {}),
        (RunIdentityDocument, {"run_id": "run-1", "written_at": AT.isoformat()}),
        (TracingConfig, {}),
    ],
)
def test_persisted_shapes_reject_unknown_fields(model, fields):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**fields, "misspelled": 1})


@pytest.mark.parametrize("values", [{"spans_dropped": -1}, {"extra": {"plugin.count": -1}}])
def test_receipt_rejects_negative_counts(values):
    with pytest.raises(ValidationError):
        ExportReceipt(**values)


def test_tracing_config_wire_shape_and_plugin_options(canon_snapshot):
    config = TracingConfig(options={"example": {"collector": "local"}})
    canon_snapshot("trial_observer_contract").assert_match(
        {"wire": config.model_dump_json()},
        "tracing_config.json",
    )
    assert TracingConfig.model_validate_json(config.model_dump_json()) == config
    for typo in ("expect_projct", "attach_budget"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TracingConfig.model_validate({typo: "wrong"})
