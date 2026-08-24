"""``GradingSubstrate`` — Protocol shape + shipped-impl contract locks.

Locks the seam ADR-0039 introduces: one Protocol, two implementations
shipped today (``in_process``, ``live_callback``), three reserved future
implementations raising ``NotImplementedError`` with a pointer to the ADR
recipe.

Also locks the entry-point group ``tolokaforge.grading_substrates`` — the
future trajectory-storage service will register itself via one entry-point
line, and this test proves discovery works.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.substrate import (
    GradingSubstrate,
    InProcessGradingSubstrate,
    LiveRunnerCallbackGradingSubstrate,
    SharedMountGradingSubstrate,
    SnapshotGradingSubstrate,
    SubstrateUnreachableError,
    TrajectoryStorageGradingSubstrate,
)

pytestmark = pytest.mark.canonical


def _in_process_fixture() -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=Path("/tmp/fake-workspace"),
        initial_state={"users": [{"id": "u1"}]},
        final_state={"users": [{"id": "u1"}, {"id": "u2"}]},
    )


class TestProtocolShape:
    """The Protocol carries exactly one question — 'how does the grader see
    the trial's state?' — and every evaluator above it depends only on the
    Protocol, never on a concrete impl. Locking the surface here prevents
    a future refactor from silently narrowing it."""

    def test_in_process_substrate_satisfies_the_protocol(self) -> None:
        assert isinstance(_in_process_fixture(), GradingSubstrate)

    def test_live_callback_substrate_satisfies_the_protocol(self) -> None:
        # Constructor takes only address + trial_id — the reads themselves
        # raise NotImplementedError until the SubstrateService lands.
        substrate = LiveRunnerCallbackGradingSubstrate(
            runner_substrate_address="grader-side:50051",
            trial_id="task:0",
        )
        assert isinstance(substrate, GradingSubstrate)


class TestInProcessSubstrate:
    """The aggregate-image / in-runner path. No transport hop; the substrate
    is a thin view over live objects the caller owns."""

    def test_reads_return_the_injected_values(self) -> None:
        db = MagicMock()
        kb = MagicMock()
        substrate = InProcessGradingSubstrate(
            db_reader=db,
            knowledge_search=kb,
            filesystem_root=Path("/tmp/ws"),
            initial_state={"orders": [{"id": 1}]},
            final_state={"orders": [{"id": 1}, {"id": 2}]},
        )
        assert substrate.db_reader() is db
        assert substrate.knowledge_search() is kb
        assert substrate.filesystem_root() == Path("/tmp/ws")
        assert substrate.initial_state() == {"orders": [{"id": 1}]}
        assert substrate.final_state() == {"orders": [{"id": 1}, {"id": 2}]}

    def test_knowledge_search_and_filesystem_root_may_be_none(self) -> None:
        """Tasks without a KB or without a workspace surface are first-class,
        not an error condition."""
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state={},
        )
        assert substrate.knowledge_search() is None
        assert substrate.filesystem_root() is None

    def test_close_is_idempotent(self) -> None:
        substrate = _in_process_fixture()
        substrate.close()
        substrate.close()  # no exception


class TestLiveCallbackSubstrateIsScaffoldedUntilServiceLands:
    """``LiveRunnerCallbackGradingSubstrate`` is the shape the deployed
    grader container will use, but its reads depend on the runner-side
    ``SubstrateService`` gRPC service which lands in a follow-up commit.

    Until then the impl raises ``NotImplementedError`` with a pointer to
    the milestone. This test locks that shape — when the follow-up lands,
    the test flips to exercise the real reads and this docstring drops."""

    def test_reads_raise_with_a_pointer_to_the_milestone(self) -> None:
        substrate = LiveRunnerCallbackGradingSubstrate(
            runner_substrate_address="grader:50051",
            trial_id="task:0",
        )
        with pytest.raises(NotImplementedError, match="#1261"):
            substrate.db_reader()
        with pytest.raises(NotImplementedError, match="#1261"):
            substrate.filesystem_root()


class TestReservedFutureSubstratesRaiseWithAdrPointer:
    """The three reserved future substrates (``TrajectoryStorage``,
    ``Snapshot``, ``SharedMount``) each raise ``NotImplementedError`` with
    a pointer to ADR-0039. A contributor reaching for them sees the recipe
    rather than a mystery stub."""

    @pytest.mark.parametrize(
        "cls",
        [TrajectoryStorageGradingSubstrate, SnapshotGradingSubstrate, SharedMountGradingSubstrate],
    )
    def test_construction_raises_with_adr_pointer(self, cls: type) -> None:
        with pytest.raises(NotImplementedError, match="ADR-0039|0039-standalone-grader"):
            cls()


class TestEntryPointGroupResolves:
    """The ``tolokaforge.grading_substrates`` entry-point group resolves
    ``in_process`` and ``live_callback`` by name. Trajectory-storage adds
    itself with a one-line entry point at land time.
    """

    def test_in_process_is_registered_and_resolves(self) -> None:
        import importlib.metadata

        eps = list(importlib.metadata.entry_points(group="tolokaforge.grading_substrates"))
        names = {ep.name for ep in eps}
        assert "in_process" in names
        assert "live_callback" in names
        in_process_ep = next(ep for ep in eps if ep.name == "in_process")
        assert in_process_ep.load() is InProcessGradingSubstrate


class TestSubstrateUnreachableError:
    """The failure surface a callback substrate raises when its source
    disappears. Distinct from a verdict of ``None`` (nothing to grade) —
    this means the grader tried and could not read the inputs it needed."""

    def test_error_type_exists_and_is_an_exception(self) -> None:
        # Constructible without args; carries a message.
        err = SubstrateUnreachableError("grader lost the runner mid-grade")
        assert "lost the runner" in str(err)
