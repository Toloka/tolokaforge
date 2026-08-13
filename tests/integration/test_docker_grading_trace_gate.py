"""A trace gate fails a trial inside a real runner container, over real gRPC.

``tests/canonical/test_grading_substrate_parity.py`` drives this same pack —
``tests/data/grading_parity/trace_checks_gate`` — through the core engine and through the
runner's ``GradeTrial`` handlers, but its runner half is an in-process servicer built from
the working tree. The image is a separately built artefact: it installs the
``tolokaforge-runner-subset`` wheel, whose file partition
(``tolokaforge/core/_runner_subset.py``) already excludes part of ``core/grading``, and
which resolves its own dependencies. Whether the shipped image can grade a gate is
therefore a fact about the image, and this is where it is read — ``RegisterTrial`` then
``GradeTrial`` over a real channel into a real container, with the verdict taken off the
wire.

The pack passes at any score (``pass_threshold: 0.0``) and its scored constraint holds in
both trials, so a gate is the only thing that can fail a trial here and the only thing
that can move the ``trace_checks`` component. Nothing is paid for: no LLM, no API key, no
``.env``.

**What this proves and what it does not.** Every ``TOOL_CALL`` event the container grades
derives from the message view the host hands it in ``llm_messages_json``. That is the
production shape — the host always sends the trajectory and the runner joins it with
whatever it recorded — but nothing inside the container produced a record here: no tool is
executed, so the record half of the timeline is empty and this suite says nothing about
it. The stronger variant, the pack's own ``read_meter`` / ``reset_meter`` driven through
``ExecuteTool`` so the container grades records it wrote itself, is unreachable: no
``mcp_server``-backed tool can start inside the runner image, because
``tolokaforge/core/tools_interface.py`` is outside the runner subset and the subset wheel
requires ``mcp>=0.1.0``, which resolves to an ``mcp`` with no ``mcp.server.fastmcp``
(#1114). Substituting a builtin tool would grade a different pack than the one this suite
exists for.
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

_TASK_ID = "trace_checks_gate"
_PARITY_GLOB = "grading_parity/**/task.yaml"
_TEST_DATA = Path(__file__).resolve().parents[1] / "data"
_PACK_DIR = _TEST_DATA / "grading_parity" / _TASK_ID

_SCORED_CONSTRAINT = "the_meter_was_read"
_GATE_CONSTRAINT = "the_meter_was_not_reset"

_CASES = ("satisfying", "violating")


def _trial_spec_json(trial_id: str, task: TaskDescription) -> str:
    """A complete ``TrialSpec``, which is what ``RegisterTrial`` validates.

    The model config is a placeholder: this pack declares no judge and runs no agent, so
    nothing reads it.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="trace_checks_gate_wire_run",
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

    ``to_task_description`` is what puts the pack's ``mcp_server.py``, its
    ``fixtures/tools.json`` and its yaml into ``TaskDescription.tool_artifacts``, which is
    what lets ``RegisterTrial`` accept the task inside the container.
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

    A suite whose subject is "the image carries this" cannot be allowed to pass against an
    image that does not. What says whose tree an image came from is its own tag list: the
    builder tags by content hash, and ``runner_container`` builds the runner image when
    ``RUNNER_IMAGE`` is absent and tags what it built as ``:latest`` — so an image this
    tree produced carries ``expected_image_ref("runner")`` beside ``:latest``, and one
    built from another tree carries a different hash. The reachable failure is a developer
    whose ``:latest`` predates their checkout (#740); CI builds, so it passes there.

    The one image this refuses that a person might defend is a current one carrying no
    content-hash tag at all — pulled, or hand-tagged. Nothing about such an image says
    which tree it came from, so it fails here too.

    The ids are written to the terminal beside the check, which a serial run shows. Under
    ``-n auto`` a worker's output never reaches the master, so on a distributed run it is
    the tag assertion — not the line — that makes a green run attributable.
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
    """Each authored case, driven as production drives one: register, grade, clean up.

    One registered trial per case, and the grade is read off the ``GradeTrial`` response
    rather than recomputed here.
    """
    grades: dict[str, dict[str, Any]] = {}
    for case in _CASES:
        trial_id = f"{_TASK_ID}_{case}:0"
        registered = runner_client.register_trial(
            trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id, task_description)
        )
        assert registered["success"] is True, registered["error"]
        try:
            result = runner_client.grade_trial(
                trial_id=trial_id,
                llm_messages_json=json.dumps(load_case(_PACK_DIR, case).runner_messages),
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


def _trace_checks_summary(grade: dict[str, Any]) -> dict[str, Any]:
    """The grade's gate report, refused when the runner sent none.

    The client maps a runner predating the field to ``None``, and a default-valued summary
    would satisfy ``gate_failed is False`` for the wrong reason.
    """
    summary = grade["trace_checks_summary"]
    assert summary is not None, (
        "the runner returned no trace_checks_summary — the image predates the field, so "
        f"nothing here reads a gate verdict: {grade['reasons']!r}"
    )
    return summary


def test_the_container_fails_the_violating_trial_on_the_gate(
    graded_cases: dict[str, dict[str, Any]],
) -> None:
    """The trial that reset the meter fails, and the grade says the gate is why.

    Every fact a reviewer would read off this grade is pinned, because the score alone
    cannot distinguish a tripped gate from a component that scored badly: the verdict, the
    zeroed component, the gate report naming the constraint that shut, the sentence in
    ``reasons`` that names it, and the per-constraint severities — which are what say the
    scored constraint passed while the gate failed, rather than both failing.
    """
    grade = graded_cases["violating"]
    summary = _trace_checks_summary(grade)

    assert grade["binary_pass"] is False
    assert grade["score"] == pytest.approx(0.0)
    assert grade["components"]["trace_checks"] == pytest.approx(0.0)
    assert summary["gate_failed"] is True
    assert summary["failed_gate_ids"] == [_GATE_CONSTRAINT]
    assert f"FAILED trace gates: {_GATE_CONSTRAINT}" in grade["reasons"], grade["reasons"]
    assert [
        (check["id"], check["severity"], check["passed"]) for check in grade["trace_checks"]
    ] == [
        (_SCORED_CONSTRAINT, "scored", True),
        (_GATE_CONSTRAINT, "gate", False),
    ]


def test_the_container_passes_the_satisfying_trial(
    graded_cases: dict[str, dict[str, Any]],
) -> None:
    """The trial that only read the meter passes, at full marks and with no gate tripped.

    This is what makes the failing case a discrimination rather than a pack that fails
    everything: one authored trajectory apart, over the same registered task.
    """
    grade = graded_cases["satisfying"]
    summary = _trace_checks_summary(grade)

    assert grade["binary_pass"] is True
    assert grade["score"] == pytest.approx(1.0)
    assert grade["components"]["trace_checks"] == pytest.approx(1.0)
    assert summary["gate_failed"] is False
    assert summary["failed_gate_ids"] == []
