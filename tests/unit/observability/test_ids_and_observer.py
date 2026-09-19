"""Id contract v1 and the observer seam (ADR-0047)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tolokaforge.observability import ids
from tolokaforge.observability.observer import (
    CompositeTrialObserver,
    ExportReceipt,
    InMemoryTrialObserver,
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


class TestPreviewKinds:
    """Contract v2's preview twins: a live row can never take a final row's id."""

    def test_every_kind_a_running_trial_reports_has_a_twin(self) -> None:
        assert set(ids.PREVIEWABLE_KINDS) == {"root", "gen", "ugen", "tool", "jgen", "jtool"}
        assert set(ids.PREVIEW_KINDS) == {"proot", "pgen", "pugen", "ptool", "pjgen", "pjtool"}
        assert ids.OBSERVATION_KINDS == ids.FINAL_KINDS | ids.PREVIEW_KINDS
        # the bundle names these; nothing reports them before it exists
        assert not ids.PREVIEW_KINDS & {"grading", "event"}

    def test_preview_kind_maps_and_refuses_a_kind_without_a_twin(self) -> None:
        assert ids.preview_kind("gen") == "pgen"
        assert ids.preview_kind("root") == "proot"
        assert ids.is_preview_kind("pgen") and not ids.is_preview_kind("gen")
        for kind in ("grading", "event", "pgen", "span"):
            with pytest.raises(ValueError):
                ids.preview_kind(kind)

    def test_a_preview_id_never_equals_its_final_id(self) -> None:
        trace = ids.trace_id(run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0)
        for kind in sorted(ids.PREVIEWABLE_KINDS):
            key = ids.ROOT_KEY if kind == "root" else "k"
            assert ids.observation_id(trace, kind, key) != ids.observation_id(
                trace, ids.preview_kind(kind), key
            )

    def test_the_formula_is_unchanged_so_no_existing_id_moves(self) -> None:
        trace = ids.trace_id(run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0)
        assert (
            ids.observation_id(trace, "pgen", 3)
            == uuid.uuid5(ids.NAMESPACE, f"obs|{trace}|pgen|3").hex[:16]
        )


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
        # the preview twins (contract v2, v4 migration): the same formula under a kind of its own
        assert identity.observation_id("proot", ids.ROOT_KEY) == "f9cd83758f7155a6"
        assert identity.observation_id("pgen", 1) == "0a3f0d2d744f553c"
        assert identity.observation_id("ptool", "call-1") == "61a323fa44935acc"

    def test_trace_and_observation_ids_derive_from_the_identity(self) -> None:
        identity = TrialIdentity(run_id="run-1", task_id="T-1", trial_index=0, attempt_id=0)
        assert identity.trace_id == ids.trace_id(
            run_tag="v1", run_id="run-1", task_id="T-1", trial_index=0, attempt=0
        )
        assert identity.root_id == ids.observation_id(identity.trace_id, "root", "-")
        with pytest.raises(ValueError, match="needs a stable key"):
            ids.observation_id(identity.trace_id, "root")


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
        recording = InMemoryTrialObserver()
        binding = LoopObserverBinding(recording, self.identity, role="agent")
        binding.generation(index=1, turn=0, request=[], result=None, started_at=None, ended_at=None)
        binding.tool_call(index=2, call=None, result=None, started_at=None, ended_at=None)
        assert [name for name, _ in recording.call_log.calls] == ["generation", "tool_call"]
        assert recording.call_log.calls[0][1]["identity"] is self.identity
        assert recording.call_log.calls[0][1]["role"] == "agent"
        assert recording.call_log.calls[1][1]["index"] == 2

    def test_binding_never_raises_into_the_loop(self) -> None:
        binding = LoopObserverBinding(
            InMemoryTrialObserver(fail_on={"generation": RuntimeError("observer exploded")}),
            self.identity,
        )
        binding.generation(index=1, turn=0, request=[], result=None, started_at=None, ended_at=None)

    def test_composite_fans_out_isolates_failures_and_sums_receipts(self) -> None:
        good = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=2, spans_exported=2, exporter="fake")
        )
        bad = InMemoryTrialObserver(
            fail_on={
                name: RuntimeError("observer exploded")
                for name in ("trial_started", "trial_finished", "run_finished")
            }
        )
        composite = CompositeTrialObserver([bad, good])
        composite.trial_started(self.identity, models={}, started_at=datetime.now(tz=timezone.utc))
        composite.trial_finished(self.identity, trajectory=None)
        assert [name for name, _ in good.call_log.calls] == ["trial_started", "trial_finished"]
        receipt = composite.run_finished()
        assert (receipt.spans_queued, receipt.spans_exported, receipt.exporter) == (2, 2, "fake")
