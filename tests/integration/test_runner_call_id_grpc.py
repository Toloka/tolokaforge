"""The tool-call id, the protocol version and the termination reason cross real
gRPC to a real runner.

What the in-process handler tests in ``tests/unit/test_runner_pipeline.py`` cannot
show is that the fields survive serialisation to a runner built from a separate
image. The discriminating assertions are the refusals: a runner that never
deserialises ``call_id`` cannot notice an empty one, a runner with no
``engine_protocol_version`` gate accepts an unversioned engine, and a runner
that ignores ``termination_reason`` grades a request naming a reason that does
not exist. Every accepting assertion here would pass against a pre-change image,
because proto3 discards a field its descriptor does not know — so the refusals
are the only half that proves the image is built from this tree.

Whether a *non-empty* id is recorded is not observable here — Stage 1 exposes no
wire read path onto the runner's recorded history — so that half is locked in
``tests/unit/test_runner_pipeline.py`` against the real handler and the real record.

Deterministic: ``calculator`` is a builtin needing no environment service, and no
LLM is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import TaskDescription
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_CALCULATOR = "calculator"


def _task_description() -> dict[str, Any]:
    """A trial graded solely on ``transcript_rules``, requiring ``calculator``.

    ``pass_threshold`` sits at 0.99 so the verdict is unambiguous: the trial
    passes only if the runner's recorded history contains the calculator calls.
    """
    return {
        "task_id": "call_id_e2e",
        "name": "Call Id Wire E2E",
        "category": "test",
        "description": "Carry the provider tool-call id to the runner over gRPC",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": [], "unstable_fields": []},
        "agent_tools": [
            {
                "name": _CALCULATOR,
                "description": "Evaluate an arithmetic expression",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"transcript_rules": 1.0},
            "pass_threshold": 0.99,
            "transcript_rules": {
                "tool_expectations": {"required_tools": [_CALCULATOR], "disallowed_tools": []}
            },
        },
    }


def _trial_spec_json(trial_id: str) -> str:
    return TrialSpec(
        trial_id=trial_id,
        run_id="call_id_e2e_run",
        task=TaskDescription.model_validate(_task_description()),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture
def runner_client(runner_container) -> GrpcRunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


class TestCallIdOverGrpc:
    def test_two_identical_calls_with_distinct_ids_both_reach_grading(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """Same tool, byte-identical arguments, two provider ids. Both calls must
        execute and the required-tool expectation must pass off the runner's own
        recorded history — so carrying an id costs the production path nothing."""
        trial_id = "call_id_e2e:0"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id)
        )
        assert registered["success"] is True, registered["error"]
        try:
            for call_id in ("toolu_A", "toolu_B"):
                executed = runner_client.execute_tool(
                    trial_id=trial_id,
                    tool_name=_CALCULATOR,
                    arguments={"expression": "2 + 2"},
                    call_id=call_id,
                )
                assert executed.success is True, executed.error
                assert "4" in executed.output, executed.output
            result = runner_client.grade_trial(trial_id=trial_id)
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)

        assert result["success"] is True, result["error"]
        assert result["grade"]["binary_pass"] is True
        assert result["grade"]["components"]["transcript_rules"] == pytest.approx(1.0)

    def test_a_call_without_an_id_is_refused_by_the_runner(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """The runner raises on an empty id rather than returning a tool-shaped
        failure the agent would retry against; over gRPC that surfaces as a failed
        RPC, so the call never records an unlinkable entry."""
        trial_id = "call_id_e2e:1"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id)
        )
        assert registered["success"] is True, registered["error"]
        try:
            executed = runner_client.execute_tool(
                trial_id=trial_id,
                tool_name=_CALCULATOR,
                arguments={"expression": "2 + 2"},
                call_id="",
            )
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)

        assert executed.success is False
        assert executed.error


class TestProtocolVersionGateOverGrpc:
    def test_an_engine_declaring_no_version_is_refused_at_registration(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """An engine predating the field sends no version, which arrives as 0. The
        registration must fail so the trial never starts and no tokens are spent.

        The request is built against the stub directly because
        :meth:`GrpcRunnerClient.register_trial` always declares the current
        version — the older engine's wire bytes are the thing under test.
        """
        trial_id = "call_id_e2e:2"
        response = runner_client.stub.RegisterTrial(
            pb2.RegisterTrialRequest(
                trial_id=trial_id,
                trial_spec_json=_trial_spec_json(trial_id),
            )
        )

        assert response.success is False
        assert "version-skewed" in response.error
        assert str(ENGINE_PROTOCOL_VERSION) in response.error

    def test_this_engine_registers_against_this_image(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """The mirror case, which is what makes the refusal above discriminating
        rather than a runner that refuses everything."""
        trial_id = "call_id_e2e:3"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id)
        )
        try:
            assert registered["success"] is True, registered["error"]
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)


class TestTerminationReasonOverGrpc:
    def test_a_reason_the_enum_does_not_name_is_refused(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """A runner that deserialises the reason refuses one it cannot parse; a
        runner that never sees the field grades the trial normally. So this is the
        assertion that tells the two images apart."""
        trial_id = "call_id_e2e:4"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id)
        )
        assert registered["success"] is True, registered["error"]
        try:
            result = runner_client.grade_trial(trial_id=trial_id, termination_reason="not_a_reason")
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)

        assert result["success"] is False
        assert "not_a_reason" in result["error"]

    def test_a_real_reason_grades_normally(self, runner_client: GrpcRunnerClient) -> None:
        """The mirror case: carrying the reason costs the production grading path
        nothing, and the trial's verdict is the one it would have had without it."""
        trial_id = "call_id_e2e:5"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id)
        )
        assert registered["success"] is True, registered["error"]
        try:
            executed = runner_client.execute_tool(
                trial_id=trial_id,
                tool_name=_CALCULATOR,
                arguments={"expression": "2 + 2"},
                call_id="toolu_C",
            )
            assert executed.success is True, executed.error
            result = runner_client.grade_trial(trial_id=trial_id, termination_reason="agent_done")
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)

        assert result["success"] is True, result["error"]
        assert result["grade"]["binary_pass"] is True
