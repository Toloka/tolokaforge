"""Contract: one trial conversation sends one session id, distinct per trial, role and attempt.

A session-affine gateway keeps a conversation on one replica by hashing the
model's session header. The value has to be stable for every request one trial's
agent (or user simulator) makes, including tool rounds and retries, and different
for every other trial, role and attempt; otherwise turns land on replicas without
the conversation's prefix cache.

Each trial here runs through :meth:`InProcessConductor._run_agent_loop` with a real
:class:`LLMClient` agent and the built-in LLM user simulator (its own real client)
against a loopback gateway. Requests are attributed to a role by the ``model`` their
body names and to a trial by running trials one after another, never by the header
under test; the gateway's catalog serves both models, so every request takes the
resolved route. The single-client cases pin the header on the wire when the catalog
is unreadable and beside the gateway's own per-request id.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.utils.recording_gateway import (
    RecordedRequest,
    RecordingGateway,
    server_error_reply,
    synthetic_error_reply,
    text_reply,
    tool_call_reply,
)
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
from tolokaforge.core.execution_mode import select_execution_mode
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    ActorSpec,
    EvaluationConfig,
    Message,
    MessageRole,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.run_display_events import _NULL_EVENTS, LLMCallObservation
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.observability.observer import TrialIdentity
from tolokaforge.runner.models import TaskDescription
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.canonical

SESSION_HEADER = "x-session-id"
AGENT_MODEL = "self-hosted/agent-canary"
USER_MODEL = "self-hosted/user-canary"
_SESSION = {"header": SESSION_HEADER}
_LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_order",
        "description": "Look an order up.",
        "parameters": {"type": "object", "properties": {"order": {"type": "integer"}}},
    },
}


@pytest.fixture
def installed_fake_secrets(serving_gateway: RecordingGateway) -> Iterator[dict[str, str]]:
    """Point the process SecretManager at the loopback gateway."""
    payload = {
        "LLM_PROXY_BASE_URL": serving_gateway.base_url,
        "LLM_PROXY_API_KEY": "sk-loopback-gateway",
    }
    with secret_manager_installed(payload):
        yield payload


@pytest.fixture
def gateway(gateway: RecordingGateway) -> RecordingGateway:
    gateway.catalog = [AGENT_MODEL, USER_MODEL]
    return gateway


class _Trial:
    """One trial attempt's identity and the wire requests each of its roles made."""

    def __init__(self, identity: TrialIdentity, requests: list[RecordedRequest]) -> None:
        self.identity = identity
        self.agent = [r for r in requests if r.model == AGENT_MODEL]
        self.user = [r for r in requests if r.model == USER_MODEL]
        assert len(self.agent) + len(self.user) == len(requests), [r.model for r in requests]

    @staticmethod
    def sent(requests: list[RecordedRequest]) -> set[str | None]:
        return {r.headers.get(SESSION_HEADER) for r in requests}


def _conductor(agent_client: LLMClient, output_dir: Path) -> InProcessConductor:
    config = RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name=AGENT_MODEL, session=_SESSION),
            "user": ModelConfig(provider="openai", name=USER_MODEL, session=_SESSION),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=2, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )
    return InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=config,
        logger=get_logger("session-header-per-conversation", strict=False),
        agent_client=agent_client,
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=output_dir,
    )


def _spec(trial_index: int, attempt_id: int) -> TrialSpec:
    task_desc = TaskDescription(
        task_id="refund",
        name="refund",
        category="test",
        description="Refund an order.",
        adapter_type="native",
        system_prompt="",
    )
    return TrialSpec(
        trial_id=f"refund:{trial_index}",
        run_id="session-run_20261001",
        attempt_id=attempt_id,
        task_id="refund",
        trial_index=trial_index,
        task=task_desc,
        execution_mode=select_execution_mode(task_desc.metadata),
        agent_model_config=ModelConfig(provider="openai", name=AGENT_MODEL, session=_SESSION),
        user_model_config=ModelConfig(provider="openai", name=USER_MODEL, session=_SESSION),
        max_turns=10,
        default_tool_timeout_s=30.0,
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


def _setup(output_dir: Path, trial_index: int) -> _TrialSetup:
    tool_executor = MagicMock()
    tool_executor.execute.return_value = ToolResult(success=True, output='{"order": 7}')
    return _TrialSetup(
        trial_id=f"refund:{trial_index}",
        trial_idx=trial_index,
        task_dir=output_dir,
        trial_dir=output_dir / "trials" / "refund" / str(trial_index),
        env_state=MagicMock(),
        adapter_env=MagicMock(),
        tool_schemas=[_LOOKUP_TOOL],
        tool_executor=tool_executor,
        user_tool_schemas=[],
        user_tool_executor=None,
    )


_TASK = TaskConfig(
    task_id="refund",
    description="Refund an order.",
    interaction_mode="conversational",
    initial_user_message="Please refund order 7.",
    actors={"user": ActorSpec(mode="llm")},
)


def _run_trial(
    conductor: InProcessConductor,
    gateway: RecordingGateway,
    output_dir: Path,
    *,
    trial_index: int,
    attempt_id: int,
) -> _Trial:
    """One trial: the agent's first request gets a synthetic-error envelope, its retry
    a tool call, the tool round the final text; the user's first request a 500 the
    OpenAI SDK re-sends, then the stop reply."""
    gateway.requests.clear()
    gateway.scripts[AGENT_MODEL] = [
        synthetic_error_reply(),
        tool_call_reply("lookup_order", {"order": 7}),
        text_reply("Order 7 is refunded."),
    ]
    gateway.scripts[USER_MODEL] = [server_error_reply(), text_reply("###STOP###")]
    spec = _spec(trial_index, attempt_id)
    setup = _setup(output_dir, trial_index)
    identity = conductor._trial_identity(spec, setup)
    with patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"):
        conductor._run_agent_loop(spec, _TASK, setup, identity)
    assert gateway.scripts == {AGENT_MODEL: [], USER_MODEL: []}, "a scripted reply went unsent"
    return _Trial(identity, list(gateway.requests))


def test_each_trial_conversation_sends_one_id_per_role(
    gateway: RecordingGateway, tmp_path: Path
) -> None:
    agent_client = LLMClient(ModelConfig(provider="openai", name=AGENT_MODEL, session=_SESSION))
    assert agent_client._gateway_route is not None
    retry_sleeps: list[float] = []
    agent_client._retry_sleep = retry_sleeps.append
    conductor = _conductor(agent_client, tmp_path)

    first = _run_trial(conductor, gateway, tmp_path, trial_index=0, attempt_id=0)
    second = _run_trial(conductor, gateway, tmp_path, trial_index=1, attempt_id=0)
    retried = _run_trial(conductor, gateway, tmp_path, trial_index=0, attempt_id=1)

    assert retry_sleeps and len(retry_sleeps) == 3, "the envelope never reached the outer retry"
    for trial in (first, second, retried):
        assert len(trial.agent) == 3, "envelope, its outer retry, and the tool round"
        assert len(trial.user) == 2, "the 500 and the OpenAI SDK's re-send"
        assert _Trial.sent(trial.agent) == {f"{trial.identity.trace_id}-agent"}
        assert _Trial.sent(trial.user) == {f"{trial.identity.trace_id}-user"}

    assert (first.identity.trial_index, second.identity.trial_index) == (0, 1)
    assert (first.identity.attempt_id, retried.identity.attempt_id) == (0, 1)
    values = [_Trial.sent(t.agent) | _Trial.sent(t.user) for t in (first, second, retried)]
    assert len(set().union(*values)) == 6, "every trial, attempt and role has its own id"


def _observation(session_id: str) -> LLMCallObservation:
    return LLMCallObservation(
        events=_NULL_EVENTS, trial_id="refund:0", role="agent", session_id=session_id
    )


def _say_ok(client: LLMClient, observation: LLMCallObservation) -> None:
    client.generate(
        system="Be terse.",
        messages=[Message(role=MessageRole.USER, content="ping")],
        observation=observation,
    )


def test_the_session_header_reaches_a_gateway_whose_catalog_is_unreadable(
    gateway: RecordingGateway,
) -> None:
    gateway.catalog = None
    client = LLMClient(ModelConfig(provider="openai", name=AGENT_MODEL, session=_SESSION))
    assert client._gateway_route is None

    _say_ok(client, _observation("conversation-1"))

    assert [(r.model, r.headers.get(SESSION_HEADER)) for r in gateway.requests] == [
        (AGENT_MODEL, "conversation-1")
    ]


def test_the_request_id_is_per_call_and_the_session_id_per_conversation(
    gateway: RecordingGateway, installed_fake_secrets: dict[str, str]
) -> None:
    """The request id is minted per outer attempt, so it is compared across
    ``generate()`` calls, never across one call's SDK re-sends."""
    with secret_manager_installed(
        {**installed_fake_secrets, "LLM_PROXY_REQUEST_ID_HEADER": "X-Request-Id"}
    ):
        client = LLMClient(ModelConfig(provider="openai", name=AGENT_MODEL, session=_SESSION))
        _say_ok(client, _observation("conversation-1"))
        _say_ok(client, _observation("conversation-1"))

    sessions = [r.headers.get(SESSION_HEADER) for r in gateway.requests]
    request_ids = [r.headers.get("x-request-id") for r in gateway.requests]
    assert sessions == ["conversation-1", "conversation-1"]
    assert all(request_ids) and request_ids[0] != request_ids[1]
