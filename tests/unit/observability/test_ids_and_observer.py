"""Id contract v1 and the observer seam (ADR-0047)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tolokaforge.observability import ids
from tolokaforge.observability.observer import (
    CompositeTrialObserver,
    ExportReceipt,
    LoopObserverBinding,
    ModelRef,
    NullTrialObserver,
    TrialIdentity,
    safely,
)

pytestmark = pytest.mark.unit


class TestIds:
    def test_trace_id_is_uuid5_over_the_pipe_joined_components(self) -> None:
        expected = uuid.uuid5(ids.NAMESPACE, "trace|v1|run-1|T-1|0|0").hex
        assert (
            ids.trace_id(run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0)
            == expected
        )
        assert len(expected) == 32

    def test_observation_id_is_the_first_16_hex_of_uuid5(self) -> None:
        trace = ids.trace_id(run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0)
        assert (
            ids.observation_id(trace, "gen", 3)
            == uuid.uuid5(ids.NAMESPACE, f"obs|{trace}|gen|3").hex[:16]
        )

    def test_attempt_changes_the_trace(self) -> None:
        base = {"run_tag": "v1", "run_id": "run-1", "task_id": "T-1", "trial_index": 0}
        assert ids.trace_id(attempt=0, **base) != ids.trace_id(attempt=1, **base)

    @pytest.mark.parametrize("bad", ["", " run", "run ", "a|b"])
    def test_components_are_validated(self, bad: str) -> None:
        with pytest.raises(ValueError):
            ids.trace_id(run_tag="v1", run_id=bad, task_id="T-1", trial_index=0, attempt=0)

    def test_unknown_kind_and_bad_trace_shape_are_refused(self) -> None:
        trace = ids.trace_id(run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0)
        with pytest.raises(ValueError):
            ids.observation_id(trace, "span", 0)
        with pytest.raises(ValueError):
            ids.observation_id("not-hex", "gen", 0)


class TestTrialIdentity:
    def test_parity_with_the_bundle_uploader(self) -> None:
        """Literals computed with langfuse-uploader's ``ids.py`` (tolokaforge-tools) for the same
        inputs: the two producers must agree byte for byte."""
        identity = TrialIdentity(run_id="run-1", task_id="T-1", trial_index=0, attempt_id=0)
        assert identity.trace_id == "3ddefa60b55e55f7b3255f309d34312e"
        # contract v2 (2026-09-16): root key "-", tool keyed by call id, judge under its grading
        assert identity.root_id == "574ae02e216d5956"
        assert identity.observation_id("gen", 1) == "0be013edd7d55929"
        assert identity.observation_id("tool", "call-1") == "72c8cce8a7315c35"
        assert identity.observation_id("tool", ids.tool_key(None, 2)) == "b3f52d400f745ba5"
        assert identity.observation_id("grading", "live:run-1") == "a73db1b441495f84"
        assert identity.observation_id("jgen", "live:run-1", 1) == "25da0f923e335e95"
        assert identity.observation_id("event", "log:0") == "453d10c7f96753eb"

    def test_trace_and_observation_ids_derive_from_the_identity(self) -> None:
        identity = TrialIdentity(run_id="run-1", task_id="T-1", trial_index=0, attempt_id=0)
        assert identity.trace_id == ids.trace_id(
            run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0
        )
        assert identity.root_id == ids.observation_id(identity.trace_id, "root", "-")
        with pytest.raises(ValueError, match="needs a stable key"):
            ids.observation_id(identity.trace_id, "root")


class _Recording:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def _record(self, name: str, kwargs: dict) -> None:
        if self.fail:
            raise RuntimeError("observer exploded")
        self.calls.append((name, kwargs))

    def trial_started(self, identity, **kwargs):
        self._record("trial_started", {"identity": identity, **kwargs})

    def generation(self, identity, **kwargs):
        self._record("generation", {"identity": identity, **kwargs})

    def tool_call(self, identity, **kwargs):
        self._record("tool_call", {"identity": identity, **kwargs})

    def trial_finished(self, identity, **kwargs):
        self._record("trial_finished", {"identity": identity, **kwargs})

    def run_finished(self):
        if self.fail:
            raise RuntimeError("observer exploded")
        return ExportReceipt(spans_queued=2, spans_exported=2, exporter="fake")


class TestObserverSeam:
    identity = TrialIdentity(run_id="run-1", task_id="T-1", trial_index=0, attempt_id=0)

    def test_null_observer_is_silent_and_reports_an_empty_receipt(self) -> None:
        null = NullTrialObserver()
        null.trial_started(
            self.identity,
            models={"agent": ModelRef("openrouter", "acme/agent-1")},
            started_at=datetime.now(tz=timezone.utc),
        )
        null.generation(self.identity, role="agent", index=1)
        null.trial_finished(self.identity, trajectory=None)
        assert null.run_finished() == ExportReceipt()

    def test_safely_swallows_and_returns_none(self) -> None:
        def boom() -> None:
            raise ValueError("no")

        assert safely(boom) is None
        assert safely(lambda x: x + 1, 1) == 2

    def test_binding_adds_identity_and_role(self) -> None:
        recording = _Recording()
        binding = LoopObserverBinding(recording, self.identity, role="agent")
        binding.generation(index=1, turn=0, request=[], result=None, started_at=None, ended_at=None)
        binding.tool_call(index=2, call=None, result=None, started_at=None, ended_at=None)
        assert [name for name, _ in recording.calls] == ["generation", "tool_call"]
        assert recording.calls[0][1]["identity"] is self.identity
        assert recording.calls[0][1]["role"] == "agent"
        assert recording.calls[1][1]["index"] == 2

    def test_binding_never_raises_into_the_loop(self) -> None:
        binding = LoopObserverBinding(_Recording(fail=True), self.identity)
        binding.generation(index=1, turn=0, request=[], result=None, started_at=None, ended_at=None)

    def test_composite_fans_out_isolates_failures_and_sums_receipts(self) -> None:
        good, bad = _Recording(), _Recording(fail=True)
        composite = CompositeTrialObserver([bad, good])
        composite.trial_started(self.identity, models={}, started_at=datetime.now(tz=timezone.utc))
        composite.trial_finished(self.identity, trajectory=None)
        assert [name for name, _ in good.calls] == ["trial_started", "trial_finished"]
        receipt = composite.run_finished()
        assert (receipt.spans_queued, receipt.spans_exported, receipt.exporter) == (2, 2, "fake")
