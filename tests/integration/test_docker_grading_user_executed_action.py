"""A real runner container grades a user-side call it executed itself.

``tests/canonical/test_user_executed_action_substrate_parity.py`` drives this same
pack — ``tests/data/grading_parity/user_executed_action`` — through the core engine
and through the runner's ``GradeTrial`` handlers, but its runner half is an
in-process servicer built from the working tree. The image is a separately built
artefact: it installs the ``tolokaforge-runner-subset`` wheel, whose file partition
(``tolokaforge/core/_runner_subset.py``) already excludes part of ``core/grading``,
and which resolves its own dependencies. Whether the shipped image can execute a
user-side tool and then grade the record it wrote is therefore a fact about the
image, and this is where it is read — ``RegisterTrial``, ``ExecuteTool`` under
``executor="user"``, then ``GradeTrial`` over a real channel into a real container,
with the verdict taken off the wire.

**What this proves and what it does not.** Both halves of the timeline come from
the container here: the ``TOOL_CALL`` event from the message view the host sends
in ``llm_messages_json``, and the record it joins with from the call the container
ran. That is what ``tests/integration/test_docker_grading_trace_gate.py`` cannot
reach — its pack's tools are ``mcp_server``-backed, and no such tool can start
inside the runner image (#1114). This pack's user tool is the ``calculator``
builtin, which the image reconstructs from the registered ``TaskDescription`` with
nothing to fetch. Nothing is paid for: no LLM, no API key, no ``.env``. What the
container is not asked to do is *decide* to make the call — the user simulator is an
LLM and runs host-side, so the call arrives as an ``ExecuteTool`` request exactly
as ``TrialRunner`` sends it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.utils.containers import RUNNER_IMAGE
from tests.utils.grading_parity_packs import load_case
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.docker import builder
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_TASK_ID = "user_executed_action"
_PARITY_GLOB = "grading_parity/**/task.yaml"
_TEST_DATA = Path(__file__).resolve().parents[1] / "data"
_PACK_DIR = _TEST_DATA / "grading_parity" / _TASK_ID

_ACTION_ID = "the_user_did_the_arithmetic"
_USER_TOOL = "calculator"

_CASES = ("satisfying", "violating")


def _trial_spec_json(trial_id: str, task: TaskDescription) -> str:
    """A complete ``TrialSpec``, which is what ``RegisterTrial`` validates.

    The model config is a placeholder: this pack declares no judge and runs no agent
    inside the container, so nothing reads it.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="user_executed_action_wire_run",
        task=task,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture(scope="module")
def task_description() -> TaskDescription:
    """The pack, resolved through the production adapter.

    ``to_task_description`` is what turns ``tools.user.enabled`` into
    ``TaskDescription.user_tools``, which is what lets the container build the
    user's registry when ``RegisterTrial`` accepts the task.
    """
    adapter = NativeAdapter({"base_dir": str(_TEST_DATA), "tasks_glob": _PARITY_GLOB})
    return adapter.to_task_description(_TASK_ID)


@pytest.fixture(scope="module")
def runner_client(runner_container) -> GrpcRunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


@pytest.fixture(scope="module", autouse=True)
def runner_image_under_test(request: pytest.FixtureRequest, runner_container) -> None:
    """Refuse to grade against an image the current tree did not build.

    A suite whose subject is "the image carries this" cannot be allowed to pass
    against an image that does not. What says whose tree an image came from is its
    own tag list: the builder tags by content hash, and ``runner_container`` builds
    the runner image when ``RUNNER_IMAGE`` is absent and tags what it built as
    ``:latest`` — so an image this tree produced carries
    ``expected_image_ref("runner")`` beside ``:latest``, and one built from another
    tree carries a different hash. The reachable failure is a developer whose
    ``:latest`` predates their checkout (#740); CI builds, so it passes there.

    The one image this refuses that a person might defend is a current one carrying
    no content-hash tag at all — pulled, or hand-tagged. Nothing about such an image
    says which tree it came from, so it fails here too.

    The ids are written to the terminal beside the check, which a serial run shows.
    Under ``-n auto`` a worker's output never reaches the master, so on a distributed
    run it is the tag assertion — not the line — that makes a green run attributable.
    """
    container_image = runner_container.get_wrapped_container().image
    expected_ref = builder.expected_image_ref("runner")
    writer = request.config.get_terminal_writer()
    writer.line(f"runner container image: {container_image.id} {container_image.tags}")
    writer.line(f"current tree's runner image ref: {expected_ref}")

    if expected_ref not in container_image.tags:
        pytest.fail(
            f"{RUNNER_IMAGE} is {container_image.id}, tagged {container_image.tags}, and "
            f"the current tree builds {expected_ref} — these trials would be graded by an "
            "image that is not this tree. Build it: uv run tolokaforge docker build --core"
        )


@pytest.fixture(scope="module")
def graded_cases(
    runner_client: GrpcRunnerClient, task_description: TaskDescription
) -> dict[str, dict[str, Any]]:
    """Each authored case, driven as production drives one: register, execute, grade.

    The authored record says which tool the user called with which arguments; the
    container produces the record itself, from an ``ExecuteTool`` request carrying
    the call id the message view declares. That id is the join, so a container that
    recorded nothing leaves the action with no evidence and the case fails on its
    own account rather than on a mismatch invented here.
    """
    grades: dict[str, dict[str, Any]] = {}
    for case in _CASES:
        trial_id = f"{_TASK_ID}_{case}:0"
        authored = load_case(_PACK_DIR, case)
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id, task_description)
        )
        assert registered["success"] is True, registered["error"]
        try:
            for record in authored.core_trajectory.tool_log:
                executed = runner_client.execute_tool(
                    trial_id=trial_id,
                    tool_name=record.tool_name,
                    arguments=record.arguments,
                    executor=record.executor.value,
                    call_id=record.call_id,
                )
                assert executed.success is True, executed.error
                assert executed.output == record.output, (
                    f"the container's {record.tool_name!r} returned {executed.output!r} where "
                    f"the pack's authored trial records {record.output!r} — the two substrates "
                    "no longer hold the same bytes"
                )
            result = runner_client.grade_trial(
                trial_id=trial_id,
                llm_messages_json=json.dumps(authored.runner_messages),
            )
        finally:
            cleaned = runner_client.cleanup_trial(trial_id=trial_id)
        # Reached only when grading propagated nothing, so a cleanup that failed is
        # reported without standing in front of the failure that caused it.
        assert cleaned["success"] is True, cleaned["error"]
        assert result["success"] is True, result["error"]
        assert result["grade"] is not None, result
        grades[case] = result["grade"]
    return grades


def test_the_container_passes_the_action_the_user_carried_out(
    graded_cases: dict[str, dict[str, Any]],
) -> None:
    """The user's own call satisfies the action, inside the image, at full marks.

    Only a record the container wrote can satisfy it: the action names
    ``requestor: user``, and the message view alone carries no executor. So a build
    whose ``ExecuteTool`` dropped the request's ``executor``, or whose user registry
    never held the tool, fails here — the call would have been refused, or recorded
    as the agent's.
    """
    grade = graded_cases["satisfying"]

    assert grade["binary_pass"] is True
    assert grade["score"] == pytest.approx(1.0)
    assert grade["components"]["transcript_rules"] == pytest.approx(1.0)


def test_the_container_fails_the_action_the_user_did_not_carry_out(
    graded_cases: dict[str, dict[str, Any]],
) -> None:
    """One argument apart, the same drive fails, and the grade says which action.

    This is what makes the passing case a discrimination rather than an image that
    passes everything: the same task, the same tool, executed the same way.
    """
    grade = graded_cases["violating"]

    assert grade["binary_pass"] is False
    assert grade["score"] == pytest.approx(0.0)
    assert grade["components"]["transcript_rules"] == pytest.approx(0.0)
    assert _ACTION_ID in grade["reasons"], grade["reasons"]
    assert _USER_TOOL in grade["reasons"], grade["reasons"]
