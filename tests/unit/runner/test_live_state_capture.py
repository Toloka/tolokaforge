"""A trial whose state lives in an MCP subprocess is graded on that state or not at all.

The db-service only mirrors such a trial; the subprocess holds what the agent's
calls changed. These tests drive a real stdio child and the real db-service app
through ``RunnerServiceImpl`` and kill the child between the agent's last call
and the final read; both reads must then fail with the cause, never answer
from the mirror.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import httpx
import pytest

from tests.utils.runner_requests import register_request, simple_task_description, trial_spec_json
from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import state_read_limits
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError
from tolokaforge.runner.service import RunnerServiceImpl, _LiveStateSync
from tolokaforge.runner.state_read_limits import StateReadDeadlines
from tolokaforge.runner.substrate_service import SubstrateServicer
from tolokaforge.runner.tool_factory import MCPServerProcess, MCPServerToolWrapper, ToolFactory

pytestmark = pytest.mark.unit

SERVER = """
import json, sys
state = json.loads(sys.argv[1]) if len(sys.argv) > 1 else None
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    params = req.get('params') or {}
    name, args = params.get('name'), params.get('arguments') or {}
    if req['method'] == 'initialize':
        result = {}
    elif name == '_tolokaforge_set_state_':
        state = json.loads(args['state_json'])
        result = {'content': [{'type': 'text', 'text': 'ok'}]}
    elif name == '_tolokaforge_get_state_':
        result = {'content': [{'type': 'text', 'text': json.dumps(state)}]}
    else:
        for user in state['users']:
            if user['id'] == args['user_id']:
                user['balance'] = args['balance']
        result = {'content': [{'type': 'text', 'text': 'balance set'}]}
    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
"""

U1_BALANCE = "$.db.users[0].balance"


def _task(*, golden_balance: int | None = None) -> dict[str, Any]:
    task = simple_task_description()
    state_checks: dict[str, Any] = {
        "jsonpath_checks": [{"path": U1_BALANCE, "equals": 120, "description": "u1 paid"}]
    }
    if golden_balance is not None:
        state_checks = {
            "hash_enabled": True,
            "golden_actions": [
                {
                    "tool_name": "set_balance",
                    "arguments": {"user_id": "u1", "balance": golden_balance},
                }
            ],
        }
    task["grading"] = {
        "combine_method": "all",
        "pass_threshold": 1.0,
        "weights": {"state_checks": 1.0},
        "state_checks": state_checks,
    }
    return task


@pytest.fixture
def trial_id() -> str:
    """The db-service app is process-wide, so every test registers its own trial."""
    return f"live_state_{uuid.uuid4().hex[:8]}:0"


@pytest.fixture
def mcp_trial(runner_service: RunnerServiceImpl, mock_grpc_context, tmp_path, trial_id):
    """A registered trial whose one agent tool runs in a real MCP child."""

    def register(
        task: dict[str, Any],
        child_tables: dict[str, Any] | None = None,
        judge_model_config: ModelConfig | None = None,
    ) -> MCPServerToolWrapper:
        spec = trial_spec_json(task, trial_id=trial_id, judge_model_config=judge_model_config)
        response = runner_service.RegisterTrial(
            register_request(spec, trial_id=trial_id), mock_grpc_context
        )
        assert response.success, response.error
        script = tmp_path / "server.py"
        script.write_text(SERVER)
        schema = {
            "name": "set_balance",
            "description": "Set a user's balance",
            "parameters": {"type": "object", "properties": {}},
            "source": {
                "toolset": "bank",
                "module_path": "server",
                "class_name": "set_balance",
                "invocation_style": "mcp_server",
                "mcp_server_script": str(script),
            },
        }
        tools = ToolFactory(runner_service.db_client, trial_id).reconstruct_tools([schema], [])
        wrapper = tools.agent_tools["set_balance"]
        assert isinstance(wrapper, MCPServerToolWrapper)
        wrapper.reset_state(child_tables or task["initial_state"]["tables"])
        runner_service.trials[trial_id].agent_tools = {"set_balance": wrapper}
        owned.append((tools, wrapper))
        return wrapper

    owned: list[Any] = []
    yield register
    for tools, wrapper in owned:
        child = wrapper._get_server().process
        try:
            tools.cleanup()
        except RuntimeError:
            # Stopping a child a test killed is its own, separate failure.
            if child.poll() is None:
                raise


def _mutate(runner_service: RunnerServiceImpl, wrapper: MCPServerToolWrapper, balance: int) -> None:
    runner_service._run_async(wrapper.execute({"user_id": "u1", "balance": balance}))


def _kill(wrapper: MCPServerToolWrapper) -> None:
    process = wrapper._get_server().process
    process.kill()
    process.wait(timeout=5)


def _hang(service: RunnerServiceImpl, wrapper: MCPServerToolWrapper, monkeypatch) -> None:
    """Make the child stop answering state reads, past a shortened sync bound."""
    monkeypatch.setattr(service, "state_read_deadlines", StateReadDeadlines(holder_read_s=0.5))
    monkeypatch.setattr(wrapper, "get_state", lambda: time.sleep(3))


def _get_state(service: RunnerServiceImpl, context, trial_id: str) -> pb2.GetStateResponse:
    return service.GetState(pb2.GetStateRequest(trial_id=trial_id, include_unstable=True), context)


def _grade(service: RunnerServiceImpl, context, trial_id: str) -> pb2.GradeTrialResponse:
    return service.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps([{"role": "assistant", "content": "done"}]),
        ),
        context,
    )


def _u1_balance(response: pb2.GetStateResponse) -> int:
    return json.loads(response.state_json)["users"][0]["balance"]


def _substrate(service: RunnerServiceImpl, trial_id: str, *, synced: bool = False):
    """The grading substrate one ``GradeTrial`` call would read through.

    ``synced`` runs that call's one MCP sync up front, so a later read failure
    is the read's own and not the sync's.
    """
    trial_context = service.trials[trial_id]
    live_state = _LiveStateSync(service, trial_id, trial_context)
    if synced:
        service._run_async(live_state.ensure())
    return service._build_grading_substrate(trial_id, trial_context, live_state)


class _RecordingRequests:
    """Passes every db-service request through and records the ``timeout`` it carried."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.sent: list[tuple[str, str, Any]] = []
        self.params: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _send(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.sent.append((method, url, kwargs.get("timeout")))
        self.params.append(dict(kwargs.get("params") or {}))
        return await getattr(self._inner, method)(url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._send("get", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._send("post", url, kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._send("patch", url, kwargs)


class TestStateReadsCarryTheirDeadline:
    """Every request a state read makes waits as long as its deadline, not the client's default."""

    def test_the_sync_get_state_final_reads_and_grading_send_the_final_read_deadline(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        deadlines = StateReadDeadlines(holder_read_s=7.0)
        monkeypatch.setattr(runner_service, "state_read_deadlines", deadlines)
        db_client = runner_service.db_client
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        assert _get_state(runner_service, mock_grpc_context, trial_id).success
        assert runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=False), mock_grpc_context
        ).success
        substrate = SubstrateServicer(runner_service)
        substrate.ReadFinalDBState(
            pb2.ReadFinalDBStateRequest(trial_id=trial_id), mock_grpc_context
        )
        substrate.ReadFinalDBStateStable(
            pb2.ReadFinalDBStateStableRequest(trial_id=trial_id), mock_grpc_context
        )
        assert _grade(runner_service, mock_grpc_context, trial_id).success

        state_requests = [
            (method, url, timeout)
            for method, url, timeout in recorder.sent
            if url.endswith(("/state", "/state/stable")) or method == "patch"
        ]
        kinds = {(method, url.rsplit("/", 1)[-1]) for method, url, _ in state_requests}
        assert {("get", "state"), ("get", "stable"), ("patch", "users")} <= kinds
        for method, url, timeout in state_requests:
            assert isinstance(timeout, httpx.Timeout), (method, url, timeout)
            # The whole-state bound, cut to what its call has left (GetState's budget
            # is that bound itself, less the time the call keeps to answer).
            assert (
                deadlines.final_state_read_s - 11.0 < timeout.read <= deadlines.final_state_read_s
            ), (method, url, timeout)
            assert timeout.connect == db_client.timeout, (method, url, timeout)

    def test_hash_grading_sends_the_final_read_deadline_on_every_request(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """The server-side hash, the snapshots, the reset and the restores read or
        write the whole state too, so none of them waits on the client's default."""
        deadlines = StateReadDeadlines(holder_read_s=7.0)
        monkeypatch.setattr(runner_service, "state_read_deadlines", deadlines)
        db_client = runner_service.db_client
        wrapper = mcp_trial(_task(golden_balance=120))
        # A mismatch, so the grading also reads both states for its diff.
        _mutate(runner_service, wrapper, 999)
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        assert _grade(runner_service, mock_grpc_context, trial_id).success

        paths = {url.split(f"/trials/{trial_id}", 1)[-1] for _, url, _ in recorder.sent}
        assert {
            "/state/hash",
            "/snapshots/pre_golden",
            "/snapshots/golden_result",
            "/snapshots/golden_result/restore",
            "/snapshots/pre_golden/restore",
            "/reset",
        } <= paths
        for method, url, timeout in recorder.sent:
            assert isinstance(timeout, httpx.Timeout), (method, url, timeout)
            assert timeout.read == deadlines.final_state_read_s, (method, url, timeout)

    def test_a_request_inside_grade_trial_is_cut_to_what_the_call_has_left(
        self, runner_service, trial_id, monkeypatch
    ) -> None:
        deadlines = StateReadDeadlines(holder_read_s=7.0)
        monkeypatch.setattr(runner_service, "state_read_deadlines", deadlines)

        with runner_service._call_budget(trial_id, 100.0):
            inside = runner_service._whole_state_read_s(trial_id)
        outside = runner_service._whole_state_read_s(trial_id)

        assert 85.0 < inside <= 90.0
        assert outside == deadlines.final_state_read_s

    def test_a_restore_after_a_failure_keeps_its_whole_budget(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """A grading that ran out of its budget must still not leave the golden world behind."""
        deadlines = StateReadDeadlines(holder_read_s=7.0)
        monkeypatch.setattr(runner_service, "state_read_deadlines", deadlines)
        db_client = runner_service.db_client
        wrapper = mcp_trial(_task())
        runner_service._run_async(db_client.create_snapshot(trial_id, "pre_golden"))
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        with runner_service._call_budget(trial_id, 0.0):
            runner_service._run_async(
                runner_service._restore_pre_golden_after_failure(trial_id, wrapper)
            )

        assert [url.rsplit("/", 1)[-1] for _, url, _ in recorder.sent] == ["restore", "state"]
        for method, url, timeout in recorder.sent:
            assert timeout.read == deadlines.final_state_read_s, (method, url, timeout)


class TestHealthyChild:
    def test_get_state_and_grade_read_what_the_agent_left(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        state = _get_state(runner_service, mock_grpc_context, trial_id)
        assert state.success, state.error
        assert _u1_balance(state) == 120

        graded = _grade(runner_service, mock_grpc_context, trial_id)
        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    def test_a_wrong_value_is_an_ordinary_failed_grade(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 999)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is False

    def test_an_untouched_world_is_graded_as_it_stands(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        mcp_trial(_task())

        state = _get_state(runner_service, mock_grpc_context, trial_id)
        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert state.success and _u1_balance(state) == 100
        assert graded.success and graded.grade.binary_pass is False

    def test_direct_grade_reads_the_child_not_an_earlier_mirror(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        """GetState synced 999; the agent's later call is what GradeTrial must see."""
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 999)
        assert _u1_balance(_get_state(runner_service, mock_grpc_context, trial_id)) == 999
        _mutate(runner_service, wrapper, 120)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    def test_hash_grading_scores_the_trial_and_hands_its_state_back(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        """The golden replay runs in the trial's own child; once it is scored, the
        child holds the agent's state again, so a later read cannot report the
        golden world as the agent's."""
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 999)

        graded = _grade(runner_service, mock_grpc_context, trial_id)
        state = _get_state(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is False
        assert state.success and _u1_balance(state) == 999

    def test_hash_grading_passes_the_golden_world(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 120)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is True


class TestChildLostBeforeTheFinalRead:
    def test_get_state_fails_with_the_cause_instead_of_the_stale_mirror(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _kill(wrapper)

        state = _get_state(runner_service, mock_grpc_context, trial_id)

        assert state.success is False
        assert "could not be synchronised" in state.error
        assert state.state_json == ""

    def test_grade_trial_is_ungradeable_not_failed(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _kill(wrapper)

        assert _get_state(runner_service, mock_grpc_context, trial_id).success is False
        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be synchronised" in graded.error
        assert not graded.HasField("grade")

    def test_a_successful_earlier_capture_does_not_certify_the_grade(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        assert _get_state(runner_service, mock_grpc_context, trial_id).success
        _mutate(runner_service, wrapper, 999)
        _kill(wrapper)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be synchronised" in graded.error

    def test_hash_grading_refuses_rather_than_hash_the_mirror(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 120)
        _kill(wrapper)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be synchronised" in graded.error

    def test_a_mirror_that_refuses_the_sync_is_not_read(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("db-service dropped the mutation")

        monkeypatch.setattr(runner_service.db_client, "mutate", refuse)

        state = _get_state(runner_service, mock_grpc_context, trial_id)
        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert state.success is False and "dropped the mutation" in state.error
        assert graded.success is False and "dropped the mutation" in graded.error

    def test_get_state_fails_on_a_hung_child(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _hang(runner_service, wrapper, monkeypatch)

        state = _get_state(runner_service, mock_grpc_context, trial_id)

        assert state.success is False
        assert "did not answer within 0.5s" in state.error
        assert "compute.state_read_timeout_s" in state.error


def _slow(wrapper: MCPServerToolWrapper, monkeypatch, delay_s: float, *, after: int = 0) -> None:
    """Make the child answer state reads ``delay_s`` late, after ``after`` prompt answers.

    A slow child is not a hung one: it answers with the state it holds, as one
    that loads a large world on first contact does once it has loaded.
    """
    real_get_state = wrapper.get_state
    prompt_answers_left = [after]

    def slow_get_state() -> dict[str, Any]:
        if prompt_answers_left[0] > 0:
            prompt_answers_left[0] -= 1
        else:
            time.sleep(delay_s)
        return real_get_state()

    monkeypatch.setattr(wrapper, "get_state", slow_get_state)


def _bound(service: RunnerServiceImpl, monkeypatch, holder_read_s: float) -> None:
    """Configure the service as ``compute.state_read_timeout_s: holder_read_s`` would."""
    monkeypatch.setattr(
        service, "state_read_deadlines", StateReadDeadlines(holder_read_s=holder_read_s)
    )


class TestSlowChild:
    """A child that answers late is read when the configured bound allows it, and only then."""

    def test_a_child_slower_than_the_bound_fails_naming_the_setting(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _bound(runner_service, monkeypatch, 0.3)
        _slow(wrapper, monkeypatch, 1.0)

        state = _get_state(runner_service, mock_grpc_context, trial_id)

        assert state.success is False
        assert "did not answer within 0.3s" in state.error
        assert "raise `compute.state_read_timeout_s`" in state.error

    def test_raising_the_bound_lets_every_reader_through(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _bound(runner_service, monkeypatch, 3.0)
        _slow(wrapper, monkeypatch, 1.0)

        state = _get_state(runner_service, mock_grpc_context, trial_id)
        detached = SubstrateServicer(runner_service).ReadFinalDBState(
            pb2.ReadFinalDBStateRequest(trial_id=trial_id), mock_grpc_context
        )
        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert state.success, state.error
        assert _u1_balance(state) == 120
        assert json.loads(detached.state_json)["users"][0]["balance"] == 120
        mock_grpc_context.set_code.assert_not_called()
        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    def test_the_read_after_the_golden_replay_is_bounded_too(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """Hash grading reads the child twice; the second read is held to the same bound."""
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 120)
        _bound(runner_service, monkeypatch, 0.5)
        _slow(wrapper, monkeypatch, 3.0, after=1)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "did not answer within 0.5s" in graded.error
        assert "compute.state_read_timeout_s" in graded.error
        assert not graded.HasField("grade")


class TestHashGradingFailsAfterTheGoldenReplay:
    """A hash grading that fails once the golden replay ran hands the trial its state back.

    Otherwise the database and the child keep the golden world, and a later
    ``GetState``, regrade or substrate read takes it for the agent's final state.
    """

    def _mirror_balance(self, service: RunnerServiceImpl, trial_id: str) -> int:
        return service._run_async(service.db_client.get_state(trial_id)).data["users"][0]["balance"]

    def test_a_refused_golden_sync_restores_the_mirror_and_the_child(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 999)
        real_sync = runner_service._sync_mcp_state_to_db
        synced: list[Any] = []

        async def refuse_the_golden_sync(trial: str, held_state: Any) -> None:
            synced.append(held_state)
            if len(synced) == 2:
                raise ConnectionError("db-service dropped the golden sync")
            await real_sync(trial, held_state)

        monkeypatch.setattr(runner_service, "_sync_mcp_state_to_db", refuse_the_golden_sync)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "dropped the golden sync" in graded.error
        assert synced[1]["users"][0]["balance"] == 120
        assert self._mirror_balance(runner_service, trial_id) == 999
        assert wrapper._get_server().get_state()["users"][0]["balance"] == 999

    def test_a_read_past_the_bound_after_the_replay_restores_the_trials_state(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch, caplog
    ) -> None:
        """The child is the slow one: its restore cannot land within the bound, so the
        failure is logged, and the restore still runs before any later read of it."""
        wrapper = mcp_trial(_task(golden_balance=120))
        _mutate(runner_service, wrapper, 999)
        real_get_state = wrapper.get_state
        _bound(runner_service, monkeypatch, 0.5)
        _slow(wrapper, monkeypatch, 2.0, after=1)

        with caplog.at_level(logging.ERROR, logger="tolokaforge.runner.service"):
            graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "did not answer within 0.5s" in graded.error
        assert "could not be restored" in caplog.text
        assert self._mirror_balance(runner_service, trial_id) == 999
        monkeypatch.setattr(wrapper, "get_state", real_get_state)
        _bound(runner_service, monkeypatch, 10.0)
        state = _get_state(runner_service, mock_grpc_context, trial_id)
        assert state.success, state.error
        assert _u1_balance(state) == 999


class _GatedHolder:
    """A state holder whose reads wait for :attr:`release`, on its own worker thread."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.release = threading.Event()
        self._state = state

    def get_state(self) -> dict[str, Any]:
        assert self.release.wait(timeout=10)
        return self._state


class TestHolderIsolation:
    def test_a_stuck_holder_holds_up_no_other_trials_read(self, runner_service) -> None:
        """A holder's blocking calls run on its own worker, not the loop's shared pool.

        The shared pool here has its one thread taken, as a few abandoned reads of
        slow holders would take a real one's: a holder read that needed it would
        wait behind them, whatever trial it belongs to.
        """
        loop = runner_service._loop
        saturated = ThreadPoolExecutor(max_workers=1)
        pool_taken = threading.Event()
        saturated.submit(pool_taken.wait, 10)
        loop.call_soon_threadsafe(loop.set_default_executor, saturated)
        stuck = _GatedHolder({"users": []})
        healthy = _GatedHolder({"users": [{"id": "u1"}]})
        healthy.release.set()
        pending = asyncio.run_coroutine_threadsafe(runner_service._read_held_state(stuck), loop)
        try:
            read = runner_service._run_async(runner_service._read_held_state(healthy), timeout=2)

            assert read == {"users": [{"id": "u1"}]}
            assert not pending.done()
        finally:
            stuck.release.set()
            pool_taken.set()
        assert pending.result(timeout=5) == {"users": []}


class TestMirrorReadFailsAfterASuccessfulSync:
    """The sync landed, but the mirror then could not be read for grading."""

    def test_a_jsonpath_read_is_ungradeable_with_the_cause(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        async def drop(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("db-service read dropped")

        monkeypatch.setattr(runner_service.db_client, "get_stable_state", drop)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be read for grading" in graded.error
        assert "read dropped" in graded.error
        assert not graded.HasField("grade")

    def test_a_raw_state_read_raises_rather_than_answer_empty(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """The custom-checks read: a lost DB is not an empty world.

        The mirror here gives no table digests, as a db-service before them, so the
        read is the mirror's whole state rather than the holder's copies the sync
        left trusted.
        """
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        async def no_digests(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(runner_service.db_client, "get_table_digests", no_digests)

        substrate = _substrate(runner_service, trial_id, synced=True)

        async def drop(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("db-service read dropped")

        monkeypatch.setattr(runner_service.db_client, "get_state", drop)

        with pytest.raises(SubstrateUnreachableError, match="could not be read for grading"):
            substrate.final_state()


class _LateJudgeReads:
    """The db-service answering the judge's reads late, cut off the way httpx cuts a request.

    A judge read (``GET .../state`` naming tables, or ``POST .../query``) answers after
    ``delay_s``. When its budget is shorter, it raises ``httpx.ReadTimeout`` at the budget
    instead. The budget is the request's own ``timeout`` when the client passes one, and the
    client's default otherwise. Every other request passes through on time, and so does
    every request the sync makes while :attr:`syncing` is set — it reads a changed table by
    name, as a judge does.
    """

    def __init__(self, inner: Any, db_client: Any, delay_s: float) -> None:
        self._inner = inner
        self._db_client = db_client
        self._delay_s = delay_s
        self.syncing = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _answer_late(self, kwargs: dict[str, Any]) -> None:
        timeout = kwargs.get("timeout") or self._db_client.timeout
        # httpx cuts a server that is slow to answer at the read phase's timeout.
        budget = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        if self._delay_s >= budget:
            await asyncio.sleep(budget)
            raise httpx.ReadTimeout("")
        await asyncio.sleep(self._delay_s)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        if (
            not self.syncing
            and url.endswith("/state")
            and (kwargs.get("params") or {}).get("tables")
        ):
            await self._answer_late(kwargs)
        return await self._inner.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/query"):
            await self._answer_late(kwargs)
        return await self._inner.post(url, **kwargs)


_JUDGE_READS = [
    pytest.param(("get_db_state", {"tables": ["users"]}), id="get_db_state"),
    pytest.param(("query_db", {"jsonpath": "$.users"}), id="query_db"),
]


class TestJudgeReadsOfTheState:
    """The judge reads the state through its own DB tools, under the same refusal.

    A task that provisions its database through ``schemas`` alone has no initial
    rows, so the judge gets no state diff and its tools are the only state read.
    """

    JUDGE = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)

    @classmethod
    def _judged_task(cls) -> tuple[dict[str, Any], dict[str, Any]]:
        task = _task()
        tables = task["initial_state"].pop("tables")
        task["grading"] = {
            "combine_method": "all",
            "pass_threshold": 1.0,
            "weights": {"llm_judge": 1.0},
            "llm_judge": {
                "rubric": {
                    "criteria": [
                        {
                            "id": "paid",
                            "description": "u1 was paid",
                            "kind": "binary",
                            "weight": 1.0,
                        }
                    ]
                }
            },
        }
        return task, tables

    def _judge_reads_then_passes(
        self, runner_service, monkeypatch, read: tuple[str, dict[str, Any]] = ("get_db_state", {})
    ) -> None:
        submit = {"reasons": "ok", "paid": True, "paid_justification": "paid\nVERDICT: MET"}
        judge = ScriptedLLMClient([[read], [("submit_report", submit)]])
        monkeypatch.setattr(runner_service._judge_model_provider, "build", lambda _config: judge)

    def test_a_healthy_child_is_judged(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        self._judge_reads_then_passes(runner_service, monkeypatch)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    def test_a_refused_tool_read_leaves_no_verdict(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        _kill(wrapper)
        self._judge_reads_then_passes(runner_service, monkeypatch)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be synchronised" in graded.error
        assert not graded.HasField("grade")

    def test_a_malformed_query_is_the_judges_to_correct(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """The db-service refusing a jsonpath is a tool error, not a lost state."""
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        self._judge_reads_then_passes(
            runner_service, monkeypatch, read=("query_db", {"jsonpath": "$.users[?(@.id=="})
        )

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    def test_a_hung_child_leaves_no_verdict(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """A child that stops answering is as lost as one that died."""
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        _hang(runner_service, wrapper, monkeypatch)
        self._judge_reads_then_passes(runner_service, monkeypatch)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "did not answer within" in graded.error
        assert not graded.HasField("grade")

    def test_a_stalled_mirror_read_leaves_no_verdict(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch
    ) -> None:
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        # The judge's read bound is the sync's bound plus the read's own budget.
        monkeypatch.setattr(state_read_limits, "_JUDGE_READ_AFTER_SYNC_S", 0.4)
        monkeypatch.setattr(
            runner_service, "state_read_deadlines", StateReadDeadlines(holder_read_s=0.1)
        )
        real_get_state = runner_service.db_client.get_state

        async def stall(trial: str, tables: Any = None, timeout: Any = None) -> Any:  # noqa: ARG001
            if tables is None:  # the sync's own read
                return await real_get_state(trial)
            await asyncio.sleep(5)

        monkeypatch.setattr(runner_service.db_client, "get_state", stall)
        self._judge_reads_then_passes(
            runner_service, monkeypatch, read=("get_db_state", {"tables": ["users"]})
        )

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "timed out after 0.5s" in graded.error

    @staticmethod
    def _late_reads(runner_service, monkeypatch, delay_s: float) -> None:
        # Deadline 0.5 s: the sync's bound plus the read's own budget.
        monkeypatch.setattr(state_read_limits, "_JUDGE_READ_AFTER_SYNC_S", 0.4)
        monkeypatch.setattr(
            runner_service, "state_read_deadlines", StateReadDeadlines(holder_read_s=0.1)
        )
        db_client = runner_service.db_client
        # The client's default, far below the deadline, as 30 s sits below a large world's.
        monkeypatch.setattr(db_client, "timeout", 0.05)
        late = _LateJudgeReads(db_client._test_client, db_client, delay_s)
        monkeypatch.setattr(db_client, "_test_client", late)
        sync = runner_service._sync_mcp_state_to_db

        async def on_time(*args: Any, **kwargs: Any) -> Any:
            late.syncing = True
            try:
                return await sync(*args, **kwargs)
            finally:
                late.syncing = False

        monkeypatch.setattr(runner_service, "_sync_mcp_state_to_db", on_time)

    @pytest.mark.parametrize("read", _JUDGE_READS)
    def test_a_read_slower_than_the_clients_default_is_judged(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch, read
    ) -> None:
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        self._late_reads(runner_service, monkeypatch, delay_s=0.2)
        self._judge_reads_then_passes(runner_service, monkeypatch, read=read)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success, graded.error
        assert graded.grade.binary_pass is True

    @pytest.mark.parametrize("read", _JUDGE_READS)
    def test_a_read_past_the_deadline_leaves_no_verdict_and_keeps_the_judges_spend(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch, read
    ) -> None:
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        self._late_reads(runner_service, monkeypatch, delay_s=2.0)
        self._judge_reads_then_passes(runner_service, monkeypatch, read=read)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be read for grading" in graded.error
        assert not graded.HasField("grade")
        # Both scripted judge calls ran before the refusal; their usage reaches the host.
        usage = json.loads(graded.failure_evidence_json)["judge_usage"]
        assert usage["calls"] == 2

    @pytest.mark.parametrize("read", _JUDGE_READS)
    def test_a_read_longer_than_its_grade_trial_has_left_fails_with_its_own_cause(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id, monkeypatch, read
    ) -> None:
        """The read's own bound outlasts the whole call: cut to what the call has
        left, it fails as a lost state with the judge's spend, not as the call's
        bare timeout."""
        task, tables = self._judged_task()
        wrapper = mcp_trial(task, child_tables=tables, judge_model_config=self.JUDGE)
        _mutate(runner_service, wrapper, 120)
        self._late_reads(runner_service, monkeypatch, delay_s=20.0)
        # The read may take 10.1 s; the whole call has 3.3 s, the last 1 s of it to answer.
        monkeypatch.setattr(state_read_limits, "_JUDGE_READ_AFTER_SYNC_S", 10.0)
        monkeypatch.setattr(state_read_limits, "_GRADE_TRIAL_AFTER_HOLDER_CALLS_S", 3.0)
        monkeypatch.setattr(state_read_limits, "_CALL_UNWIND_S", 1.0)
        monkeypatch.setattr(state_read_limits, "_MIN_STATE_REQUEST_S", 0.05)
        self._judge_reads_then_passes(runner_service, monkeypatch, read=read)

        graded = _grade(runner_service, mock_grpc_context, trial_id)

        assert graded.success is False
        assert "could not be read for grading" in graded.error
        usage = json.loads(graded.failure_evidence_json)["judge_usage"]
        assert usage["calls"] == 2


class TestNoDatabase:
    """A trial that provisions no database has no mirror for its MCP tools to sync."""

    @staticmethod
    def _task_without_db() -> tuple[dict[str, Any], dict[str, Any]]:
        task = _task()
        tables = task["initial_state"]["tables"]
        task["initial_state"] = {}
        return task, tables

    def test_final_reads_keep_the_no_database_answer(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        task, tables = self._task_without_db()
        wrapper = mcp_trial(task, child_tables=tables)
        _mutate(runner_service, wrapper, 120)

        answer = SubstrateServicer(runner_service).ReadFinalDBState(
            pb2.ReadFinalDBStateRequest(trial_id=trial_id), mock_grpc_context
        )

        assert answer.trial_not_found is True
        mock_grpc_context.set_code.assert_not_called()

    def test_grading_reads_get_the_db_clients_own_error(
        self, runner_service, mcp_trial, trial_id
    ) -> None:
        """The error evaluators read as "no database", not an unreachable substrate."""
        task, tables = self._task_without_db()
        wrapper = mcp_trial(task, child_tables=tables)
        _mutate(runner_service, wrapper, 120)

        with pytest.raises(DBTrialNotFoundError):
            _substrate(runner_service, trial_id).final_state()


class TestDetachedGraderReads:
    """The grader outside the runner reads final state through ``SubstrateService``."""

    def test_final_reads_come_from_the_child(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        servicer = SubstrateServicer(runner_service)

        raw = servicer.ReadFinalDBState(
            pb2.ReadFinalDBStateRequest(trial_id=trial_id), mock_grpc_context
        )
        stable = servicer.ReadFinalDBStateStable(
            pb2.ReadFinalDBStateStableRequest(trial_id=trial_id), mock_grpc_context
        )

        assert json.loads(raw.state_json)["users"][0]["balance"] == 120
        assert json.loads(stable.state_json)["users"][0]["balance"] == 120
        mock_grpc_context.set_code.assert_not_called()

    def test_a_lost_child_answers_unavailable(
        self, runner_service, mock_grpc_context, mcp_trial, trial_id
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        _kill(wrapper)
        servicer = SubstrateServicer(runner_service)

        answer = servicer.ReadFinalDBStateStable(
            pb2.ReadFinalDBStateStableRequest(trial_id=trial_id), mock_grpc_context
        )

        assert answer.state_json == ""
        mock_grpc_context.set_code.assert_called_once_with(grpc.StatusCode.UNAVAILABLE)
        (details,) = mock_grpc_context.set_details.call_args.args
        assert "could not be synchronised" in details

    def test_a_lost_database_of_a_held_trial_answers_unavailable(
        self, runner_service, mock_grpc_context, trial_id
    ) -> None:
        """Without an MCP child to sync, the read itself must not turn a lost DB into ``{}``."""
        registered = runner_service.RegisterTrial(
            register_request(trial_spec_json(_task(), trial_id=trial_id), trial_id=trial_id),
            mock_grpc_context,
        )
        assert registered.success, registered.error
        runner_service._run_async(runner_service.db_client.delete_trial(trial_id))

        answer = SubstrateServicer(runner_service).ReadFinalDBStateStable(
            pb2.ReadFinalDBStateStableRequest(trial_id=trial_id), mock_grpc_context
        )

        assert answer.trial_not_found is False and answer.state_json == ""
        mock_grpc_context.set_code.assert_called_once_with(grpc.StatusCode.UNAVAILABLE)
        (details,) = mock_grpc_context.set_details.call_args.args
        assert "could not be read for grading" in details

    def test_a_trial_this_runner_does_not_hold_is_not_found(
        self, runner_service, mock_grpc_context, trial_id
    ) -> None:
        answer = SubstrateServicer(runner_service).ReadFinalDBState(
            pb2.ReadFinalDBStateRequest(trial_id=trial_id), mock_grpc_context
        )

        assert answer.trial_not_found is True
        mock_grpc_context.set_code.assert_not_called()


class TestStateAnswerShape:
    """``_tolokaforge_get_state_`` answers are read strictly: only a state object is state."""

    @pytest.mark.parametrize(
        ("result", "match"),
        [
            ({"isError": True, "content": [{"type": "text", "text": "boom"}]}, "declared failure"),
            ({}, "no state text"),
            ({"content": []}, "no state text"),
            ({"content": [{"type": "text", "text": "[]"}]}, "not a state object"),
            ({"content": ["{}"]}, "no state text"),
            ({"content": [{"type": "text", "text": "not json"}]}, "unreadable state"),
        ],
    )
    def test_a_malformed_answer_raises(self, monkeypatch, result: dict, match: str) -> None:
        monkeypatch.setattr(MCPServerProcess, "send_request", lambda *_a, **_k: result)

        with pytest.raises(RuntimeError, match=match):
            MCPServerProcess(script_path="/unused.py").get_state()

    def test_a_refused_state_hand_back_raises(self, monkeypatch) -> None:
        """A child that kept its old state must not pass for one that took the new."""
        answer = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
        monkeypatch.setattr(MCPServerProcess, "send_request", lambda *_a, **_k: answer)

        with pytest.raises(RuntimeError, match="_tolokaforge_set_state_ declared failure"):
            MCPServerProcess(script_path="/unused.py").reset_state({"users": []})

    def test_an_empty_state_object_is_a_state(self, monkeypatch) -> None:
        answer = {"content": [{"type": "text", "text": "{}"}]}
        monkeypatch.setattr(MCPServerProcess, "send_request", lambda *_a, **_k: answer)

        assert MCPServerProcess(script_path="/unused.py").get_state() == {}


class TestTheSyncReadsOnlyTheTablesThatDiffer:
    """The mirror's table digests decide what the sync and the final read fetch."""

    @staticmethod
    def _rows_read(recorder: _RecordingRequests) -> list[str]:
        """The table selections of every state read: ``*`` for a whole-state read."""
        return [
            params.get("tables") or "*"
            for (method, url, _), params in zip(recorder.sent, recorder.params, strict=True)
            if method == "get" and url.endswith("/state")
        ]

    def test_a_second_sync_of_the_same_state_reads_no_rows(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        first = runner_service._run_async(runner_service._refresh_db_mirror(trial_id))
        assert first is not None and first.trusted == frozenset(first.order or ())
        db_client = runner_service.db_client
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        second = runner_service._run_async(runner_service._refresh_db_mirror(trial_id))

        assert self._rows_read(recorder) == []
        assert [method for method, _, _ in recorder.sent] == ["get"]  # the digests alone
        assert second is not None and second.trusted == first.trusted

    def test_a_changed_table_is_the_only_one_read_back(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        task = _task()
        task["initial_state"]["tables"]["ledger"] = [{"id": "e1", "amount": 5}]
        wrapper = mcp_trial(task)
        db_client = runner_service.db_client
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)
        _mutate(runner_service, wrapper, 120)

        runner_service._run_async(runner_service._refresh_db_mirror(trial_id))

        assert self._rows_read(recorder) == ["users"]
        assert [
            url.rsplit("/", 1)[-1] for method, url, _ in recorder.sent if method == "patch"
        ] == ["users"]

    def test_the_final_state_is_the_held_copy_and_equals_the_mirror(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        substrate = _substrate(runner_service, trial_id, synced=True)
        db_client = runner_service.db_client
        mirror = runner_service._run_async(db_client.get_state(trial_id)).data
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        final = substrate.final_state()

        assert self._rows_read(recorder) == []
        assert final == mirror
        assert list(final) == list(mirror)
        assert final["users"][0]["balance"] == 120

    def test_a_table_only_the_mirror_holds_is_read_from_it_in_the_mirrors_order(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        task = _task()
        task["initial_state"]["tables"] = {
            "audit": [{"id": "a1", "by": "seed"}],
            **task["initial_state"]["tables"],
        }
        # The child holds every table but the audit, which only the mirror has.
        held = {k: v for k, v in task["initial_state"]["tables"].items() if k != "audit"}
        wrapper = mcp_trial(task, child_tables=held)
        _mutate(runner_service, wrapper, 120)
        substrate = _substrate(runner_service, trial_id, synced=True)
        db_client = runner_service.db_client
        mirror = runner_service._run_async(db_client.get_state(trial_id)).data
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        final = substrate.final_state()

        assert self._rows_read(recorder) == ["audit"]
        assert final == mirror and list(final) == list(mirror)
        assert list(final)[0] == "audit"

    def test_a_table_the_mirror_stores_otherwise_is_read_from_the_mirror(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        """A sync whose mutations leave the mirror's JSON unlike the holder's — an int the
        holder keeps as a float, equal in Python and so never mutated — trusts nothing it
        cannot vouch for."""
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        runner_service._run_async(runner_service._refresh_db_mirror(trial_id))
        held = wrapper.get_state()
        held["users"][0]["balance"] = 120.0
        monkeypatch.setattr(wrapper, "get_state", lambda: held)
        substrate = _substrate(runner_service, trial_id, synced=True)
        db_client = runner_service.db_client
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        final = substrate.final_state()

        assert self._rows_read(recorder) == ["users"]
        assert type(final["users"][0]["balance"]) is int

    def test_a_db_service_without_digests_syncs_and_reads_the_whole_state(
        self, runner_service, mcp_trial, trial_id, monkeypatch
    ) -> None:
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)
        db_client = runner_service.db_client

        async def no_digests(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(db_client, "get_table_digests", no_digests)
        recorder = _RecordingRequests(db_client._test_client)
        monkeypatch.setattr(db_client, "_test_client", recorder)

        substrate = _substrate(runner_service, trial_id, synced=True)
        final = substrate.final_state()

        assert self._rows_read(recorder) == ["*", "*"]
        assert final["users"][0]["balance"] == 120
