"""``GradingSubstrate`` — Protocol shape + shipped-impl contract locks.

Locks the seam ADR-0039 introduces: one Protocol, two implementations
shipped today (``in_process``, ``live_callback``), three reserved future
implementations raising ``NotImplementedError`` with a pointer to the ADR
recipe.

Also locks the entry-point group ``tolokaforge.grading_substrates`` — the
future trajectory-storage service will register itself via one entry-point
line, and this test proves discovery works.

``TestLiveCallbackSubstrateReads`` exercises the live-callback impl over an
in-process gRPC channel wired to a real :class:`RunnerServiceImpl` +
:class:`SubstrateServicer`, asserting the wire path returns the same values
:class:`InProcessGradingSubstrate` would over the same runner.
"""

from __future__ import annotations

from concurrent import futures
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.grading.substrate import (
    GradingSubstrate,
    InProcessGradingSubstrate,
    LiveRunnerCallbackGradingSubstrate,
    SharedMountGradingSubstrate,
    SnapshotGradingSubstrate,
    SubstrateUnreachableError,
    TrajectoryStorageGradingSubstrate,
)
from tolokaforge.runner import (
    add_RunnerServiceServicer_to_server,
    add_SubstrateServiceServicer_to_server,
)
from tolokaforge.runner.models import (
    RunnerInitialStateConfig,
    StableStateResponse,
    StateResponse,
    TaskDescription,
)
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.substrate_service import SubstrateServicer

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
        # Constructor opens a lazy gRPC channel; the isinstance check does
        # not dial the wire. Close the substrate to release the channel.
        substrate = LiveRunnerCallbackGradingSubstrate(
            runner_substrate_address="grader-side:50051",
            trial_id="task:0",
        )
        try:
            assert isinstance(substrate, GradingSubstrate)
        finally:
            substrate.close()


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

    def test_final_state_stable_reads_the_stable_factory_output(self) -> None:
        """STABLE jsonpath reads route through the stable factory — the
        server-side unstable-field filter the runner's ``get_stable_state``
        applies is what jsonpath scoring resolves against."""
        stable_calls = 0

        def factory() -> dict[str, Any]:
            nonlocal stable_calls
            stable_calls += 1
            return {"users": [{"id": "u1", "name": "Alice"}]}

        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={"users": [{"id": "u1"}]},
            final_state={"users": [{"id": "u1", "name": "Alice", "token": "opaque"}]},
            final_state_stable_factory=factory,
        )

        assert substrate.final_state_stable() == {"users": [{"id": "u1", "name": "Alice"}]}
        # Memoised — a second read reuses the first factory answer.
        assert substrate.final_state_stable() == {"users": [{"id": "u1", "name": "Alice"}]}
        assert stable_calls == 1
        # The RAW view carries fields the STABLE view filtered out.
        assert substrate.final_state() == {
            "users": [{"id": "u1", "name": "Alice", "token": "opaque"}]
        }

    def test_filesystem_state_returns_the_factorys_map_when_wired(self) -> None:
        walked = {"/env/fs/agent-visible/notes.md": "# hello"}
        calls = 0

        def factory() -> dict[str, str]:
            nonlocal calls
            calls += 1
            return walked

        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=Path("/tmp/ws"),
            initial_state={},
            final_state={},
            filesystem_state_factory=factory,
        )

        assert substrate.filesystem_state() == walked
        # Memoised — a second read reuses the first factory answer.
        assert substrate.filesystem_state() == walked
        assert calls == 1

    def test_filesystem_state_returns_empty_when_the_factory_returns_no_files(
        self,
    ) -> None:
        """A workspace that exists but holds no readable files maps to ``{}`` —
        the composite's jsonpath state carries an empty ``$.filesystem`` and
        every ``$.filesystem['/env/fs/agent-visible/<rel>']`` resolves to nothing."""
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=Path("/tmp/ws"),
            initial_state={},
            final_state={},
            filesystem_state_factory=lambda: {},
        )
        assert substrate.filesystem_state() == {}

    def test_filesystem_state_returns_none_when_no_factory_is_wired(self) -> None:
        """The Protocol's ``None`` answer — 'this trial declared no filesystem
        surface' — surfaces via the absent factory. Distinct from ``{}`` (a
        workspace root that exists but holds no readable files)."""
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state={},
        )
        assert substrate.filesystem_state() is None

    def test_final_state_and_final_state_factory_together_raise_at_construction(
        self,
    ) -> None:
        """The two argument shapes for :meth:`final_state` are mutually exclusive —
        supplying both is a construction-time ``ValueError`` naming both fields."""
        with pytest.raises(ValueError, match="final_state.*final_state_factory"):
            InProcessGradingSubstrate(
                db_reader=MagicMock(),
                knowledge_search=None,
                filesystem_root=None,
                initial_state={},
                final_state={"users": [{"id": "u1"}]},
                final_state_factory=lambda: {"users": [{"id": "u2"}]},
            )

    def test_final_state_stable_without_wiring_raises_fail_loud(self) -> None:
        """A caller reaching for STABLE without wiring is a bug — the substrate
        never silently returns RAW rows."""
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state={"users": [{"id": "u1"}]},
        )
        with pytest.raises(RuntimeError, match="final_state_stable_factory"):
            substrate.final_state_stable()

    def test_final_state_factory_memoises_across_calls(self) -> None:
        calls = 0

        def factory() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"users": [{"id": "u1"}]}

        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_factory=factory,
        )
        assert substrate.final_state() == {"users": [{"id": "u1"}]}
        assert substrate.final_state() == {"users": [{"id": "u1"}]}
        assert calls == 1


_TRIAL_ID = "task:0"
_INITIAL_TABLES = {"users": [{"id": "u1", "name": "Alice"}]}
_RAW_FINAL_TABLES = {"users": [{"id": "u1", "name": "Alice", "session_token": "S-1"}]}
_STABLE_FINAL_TABLES = {"users": [{"id": "u1", "name": "Alice"}]}


def _minimal_task_description() -> TaskDescription:
    return TaskDescription.model_validate(
        {
            "task_id": "callback_substrate_e2e",
            "name": "Callback substrate reads",
            "category": "test",
            "description": "In-process gRPC LiveRunnerCallback substrate",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_INITIAL_TABLES).model_dump(),
            "agent_tools": [],
            "user_tools": [],
        }
    )


class _FakeDBServiceClient:
    """Async DB client stand-in tallying the wire calls the substrate makes.

    The runner service's ``_run_async`` bridges these onto its dedicated loop
    exactly the way it bridges the real :class:`DBServiceClient`; the fake
    exposes only the two endpoints the substrate service reads.
    """

    def __init__(
        self,
        *,
        raw: dict[str, list[dict[str, Any]]],
        stable: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._raw = raw
        self._stable = stable
        self.raw_calls = 0
        self.stable_calls = 0

    async def get_state(
        self,
        trial_id: str,  # noqa: ARG002
        tables: list[str] | None = None,  # noqa: ARG002
    ) -> StateResponse:
        self.raw_calls += 1
        return StateResponse(data=self._raw, version=1, full_hash="full", stable_hash="stable")

    async def get_stable_state(self, trial_id: str) -> StableStateResponse:  # noqa: ARG002
        self.stable_calls += 1
        return StableStateResponse(
            data=self._stable, version=1, stable_hash="stable", filtered_fields=[]
        )

    async def health_check(self) -> Any:
        raise AssertionError("LiveCallback test does not exercise health_check")

    async def close(self) -> None:
        return None


class _FakeKnowledgeSearch:
    """Deterministic :class:`KnowledgeSearch` for the KB-provisioned case."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        self.calls.append((query, top_k, alpha))
        return list(self._hits)


@contextmanager
def _running_runner(
    *,
    fake_db: _FakeDBServiceClient,
    kb: _FakeKnowledgeSearch | None,
    workspace_root: Path | None,
    monkeypatch: pytest.MonkeyPatch,
):
    """Bring up an in-process gRPC server carrying ``RunnerService`` +
    ``SubstrateService`` with the substrate flag on, a fake DB client, and
    (optionally) a workspace root pointed at ``tmp_path``. Yields the runner,
    the trial context, and a connected channel."""
    if workspace_root is not None:
        monkeypatch.setattr(
            "tolokaforge.runner.service.AGENT_WORK_DIR", str(workspace_root), raising=False
        )
    runner = RunnerServiceImpl(db_client=fake_db)  # type: ignore[arg-type]
    trial_context = TrialContextRuntime(
        trial_id=_TRIAL_ID, task_description=_minimal_task_description()
    )
    if kb is not None:
        trial_context.register_kb_search(kb)
    runner.trials[_TRIAL_ID] = trial_context

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_RunnerServiceServicer_to_server(runner, server)
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            yield runner, trial_context, channel, server
    finally:
        server.stop(grace=None)
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)


class TestLiveCallbackSubstrateReads:
    """Every LiveCallback read returns the same value :class:`InProcessGrading
    Substrate` would over the same runner — the parity claim Stage 6's gate
    will drive against a full grading pipeline. Locked here per-accessor over
    an in-process gRPC channel so a wire drift in the servicer or the client
    surfaces at the substrate seam."""

    def test_initial_state_matches_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                in_process = InProcessGradingSubstrate(
                    db_reader=MagicMock(),
                    knowledge_search=None,
                    filesystem_root=None,
                    initial_state=_INITIAL_TABLES,
                    final_state=_RAW_FINAL_TABLES,
                )
                assert substrate.initial_state() == in_process.initial_state()
            finally:
                substrate.close()

    def test_final_state_matches_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                in_process = InProcessGradingSubstrate(
                    db_reader=MagicMock(),
                    knowledge_search=None,
                    filesystem_root=None,
                    initial_state=_INITIAL_TABLES,
                    final_state=_RAW_FINAL_TABLES,
                )
                assert substrate.final_state() == in_process.final_state()
                assert substrate.final_state() == _RAW_FINAL_TABLES
            finally:
                substrate.close()

    def test_db_reader_get_state_matches_the_runners_raw_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                assert substrate.db_reader().get_state() == _RAW_FINAL_TABLES
            finally:
                substrate.close()

    def test_knowledge_search_returns_none_when_no_kb_is_provisioned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                assert substrate.knowledge_search() is None
            finally:
                substrate.close()

    def test_knowledge_search_matches_in_process_hits_when_kb_is_provisioned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = [
            SearchHit(doc_id="d1", source="rag", score=0.9, text="Alice manual"),
            SearchHit(doc_id="d2", source="rag", score=0.5, text="Bob manual"),
        ]
        kb = _FakeKnowledgeSearch(hits)
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=kb, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                live_kb = substrate.knowledge_search()
                assert live_kb is not None
                in_process = InProcessGradingSubstrate(
                    db_reader=MagicMock(),
                    knowledge_search=kb,
                    filesystem_root=None,
                    initial_state={},
                    final_state={},
                )
                in_process_kb = in_process.knowledge_search()
                assert in_process_kb is not None
                assert live_kb.search("Alice") == in_process_kb.search("Alice")
            finally:
                substrate.close()

    def test_filesystem_root_matches_read_agent_visible_filesystem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "one.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "top.md").write_text("# top", encoding="utf-8")

        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=tmp_path, monkeypatch=monkeypatch
        ) as (runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                root = substrate.filesystem_root()
                assert root is not None
                materialised = {
                    p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
                    for p in root.rglob("*")
                    if p.is_file()
                }
                shipped = {
                    key.removeprefix("/env/fs/agent-visible/"): value
                    for key, value in runner._read_agent_visible_filesystem().items()
                }
                assert materialised == shipped
                assert materialised == {"top.md": "# top", "notes/one.txt": "hello"}
            finally:
                substrate.close()

    def test_filesystem_root_is_none_when_the_workspace_root_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist"
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=missing, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                assert substrate.filesystem_root() is None
            finally:
                substrate.close()

    def test_reads_are_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, _server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                substrate.final_state()
                substrate.final_state()
                substrate.final_state_stable()
                substrate.final_state_stable()
            finally:
                substrate.close()
        assert fake_db.raw_calls == 1
        assert fake_db.stable_calls == 1

    def test_shutdown_mid_grade_raises_substrate_unreachable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_db = _FakeDBServiceClient(raw=_RAW_FINAL_TABLES, stable=_STABLE_FINAL_TABLES)
        with _running_runner(
            fake_db=fake_db, kb=None, workspace_root=None, monkeypatch=monkeypatch
        ) as (_runner, _trial, channel, server):
            substrate = LiveRunnerCallbackGradingSubstrate(
                runner_substrate_address="unused", trial_id=_TRIAL_ID, channel=channel
            )
            try:
                assert substrate.initial_state() == _INITIAL_TABLES
                server.stop(grace=None)
                # LiveCallback caches on success; force a fresh RPC by
                # reaching for an accessor that has not yet dialled the wire.
                with pytest.raises(SubstrateUnreachableError):
                    substrate.final_state()
            finally:
                substrate.close()


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
