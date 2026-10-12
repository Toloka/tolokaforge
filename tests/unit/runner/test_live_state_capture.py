"""A trial whose state lives in an MCP subprocess is graded on that state or not at all.

The db-service only mirrors such a trial; the subprocess holds what the agent's
calls changed. These tests drive a real stdio child and the real db-service app
through ``RunnerServiceImpl`` and kill the child between the agent's last call
and the final read; both reads must then fail with the cause, never answer
from the mirror.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import grpc
import pytest

from tests.utils.runner_requests import register_request, simple_task_description, trial_spec_json
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError
from tolokaforge.runner.service import RunnerServiceImpl, _LiveStateSync
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
        task: dict[str, Any], child_tables: dict[str, Any] | None = None
    ) -> MCPServerToolWrapper:
        response = runner_service.RegisterTrial(
            register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id),
            mock_grpc_context,
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
        """The custom-checks read: a lost DB is not an empty world."""
        wrapper = mcp_trial(_task())
        _mutate(runner_service, wrapper, 120)

        substrate = _substrate(runner_service, trial_id, synced=True)

        async def drop(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("db-service read dropped")

        monkeypatch.setattr(runner_service.db_client, "get_state", drop)

        with pytest.raises(SubstrateUnreachableError, match="could not be read for grading"):
            substrate.final_state()


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


class TestStateAnswerShape:
    """``_tolokaforge_get_state_`` answers are read strictly: only a state object is state."""

    @pytest.mark.parametrize(
        ("result", "match"),
        [
            ({"isError": True, "content": [{"type": "text", "text": "boom"}]}, "declared failure"),
            ({}, "no state text"),
            ({"content": []}, "no state text"),
            ({"content": [{"type": "text", "text": "[]"}]}, "not a state object"),
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
