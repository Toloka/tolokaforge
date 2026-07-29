"""Live bring-up of the standalone compose recipe, two image-source modes.

``deploy/standalone/docker-compose.yaml`` is the shipped reference recipe. This
suite stands the whole four-service stack up with ``docker compose up`` — no
mocked compose — and asserts the behaviour a cold user depends on: every service
reaches Docker ``healthy`` and the runner answers a real ``HealthCheck`` RPC with
a serving status wired through to db-service.

Two modes drive the same recipe:

- **local** (every PR, keyless): builds the four images from the current tree and
  tags each ``tolokasoft1/tolokaforge-<component>:local``, then brings the stack
  up with ``TOLOKAFORGE_IMAGE_TAG=local``. This is the always-runs behaviour lock.
- **published** (nightly/release): pulls ``tolokasoft1/tolokaforge-*:latest`` and
  brings the same recipe up against it; skip-guarded on tag availability (absent
  until the first stable publish), mirroring the parity suite's published source.

Two gated tests each drive one real trial through the composed runner over the
ADR-0024 ``run-trial`` exec wire (``requires_api``): a bundled db-service trial
(the ``tool_use`` pack) and a mock-web booking trial (``http_request`` → mock-web),
each proving its peer functionally participates in a graded ``TrialResult`` through
the stack.

``docker compose up --wait`` blocks on all four healthchecks. rag-service loads
``all-MiniLM-L6-v2`` eagerly and downloads it from HuggingFace on a cold cache, so
the wait floor is 300s — the local lane inherits the same HF-download/network
dependency the repo's existing rag Docker tests already tolerate (their health
timeout is 180s).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.deploy.conftest import (
    IMAGE_COMPONENTS,
    REPO_ROOT,
    StackHandle,
    build_and_tag_local,
    compose,
    pull_published,
)
from tests.utils.docker_helpers import wait_for_health

if TYPE_CHECKING:
    from tolokaforge.core.models import Trajectory
    from tolokaforge.core.trial import TrialResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.slow,
]

# rag-service's cold-start HuggingFace model download can take well over two
# minutes; floor the compose wait above the repo's existing 180s rag health
# timeout so `up --wait` does not trip on a cold cache.
_COMPOSE_WAIT_TIMEOUT_S = 300

_RUNNER_ADDR = "localhost:50051"

# A bundled example that exercises db-service (db_query/db_update) plus the
# filesystem tools — a real trial genuinely routed through the composed stack.
_PAID_TASK = (
    REPO_ROOT
    / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)

# A native http_request-only pack that books a room on mock-web and reports the
# site-issued confirmation number — the trial that functionally drives mock-web.
_MOCK_WEB_TASK = REPO_ROOT / "examples/native/mock_web_booking/dataset/tasks/booking_01/task.yaml"


@pytest.fixture(scope="module", params=["local", "published"])
def composed_stack(request: pytest.FixtureRequest, docker_daemon: None) -> Iterator[StackHandle]:
    """Bring the standalone recipe up in the requested image-source mode.

    Local mode always runs (builds + tags ``:local``); published mode skips until
    the first publish makes ``:latest`` pullable. The stack is torn down with
    ``down -v`` regardless of assertion outcome.
    """
    mode: str = request.param
    tag = "local" if mode == "local" else "latest"
    if mode == "local":
        build_and_tag_local()
    elif not pull_published(tag):
        pytest.skip("tolokasoft1/tolokaforge-*:latest not available until the first publish")

    project = f"tf-standalone-{mode}"
    up = compose(
        project,
        ["up", "-d", "--wait", "--wait-timeout", str(_COMPOSE_WAIT_TIMEOUT_S)],
        tag,
    )
    try:
        assert up.returncode == 0, (
            f"`compose up --wait` failed for {mode} mode "
            f"(rc={up.returncode}):\n{up.stdout}\n{up.stderr}"
        )
        yield StackHandle(mode=mode, project=project, tag=tag)
    finally:
        compose(project, ["down", "-v"], tag)


def _service_container_id(handle: StackHandle, component: str) -> str:
    proc = compose(handle.project, ["ps", "-q", component], handle.tag)
    container_id = proc.stdout.strip()
    assert container_id, f"{component} has no running container in project {handle.project}"
    return container_id


def test_stack_all_services_healthy(composed_stack: StackHandle) -> None:
    """Every service in the composed stack reaches Docker ``healthy``."""
    for component in IMAGE_COMPONENTS:
        container_id = _service_container_id(composed_stack, component)
        status = wait_for_health(container_id, timeout_s=_COMPOSE_WAIT_TIMEOUT_S)
        assert status == "healthy", f"{component} never became healthy (last status: {status!r})"


def test_stack_runner_health_check_serving(composed_stack: StackHandle) -> None:
    """The runner answers a real ``HealthCheck`` RPC, serving and db-connected."""
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient

    client = GrpcRunnerClient(_RUNNER_ADDR)
    client.connect(timeout=30)
    try:
        health = client.health_check_detailed()
    finally:
        client.close()
    assert health["status"] == "healthy", f"runner HealthCheck not serving: {health}"
    assert health["db_service_connected"], f"runner reports db-service disconnected: {health}"


def _agent_posted_to_mock_web(trajectory: Trajectory) -> bool:
    """True when an assistant ``http_request`` tool call POSTed to mock-web.

    Walks the assistant tool calls the grader's ``required_actions`` gate scores
    (``Message.tool_calls`` on assistant messages) and matches the booking
    submission itself: ``name == "http_request"``, ``method == "POST"``, target
    host ``mock-web``. This is the round-trip, not its echo in the seed message.
    """
    from tolokaforge.core.models import MessageRole

    for message in trajectory.messages:
        if message.role is not MessageRole.ASSISTANT or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if call.name == "http_request" and call.arguments.get("method") == "POST":
                if "mock-web" in str(call.arguments.get("url", "")):
                    return True
    return False


def _pick_provider() -> tuple[str, str] | None:
    """(provider, model) whose credential is available via ``SecretManager``."""
    from tolokaforge.secrets import get_default

    secrets = get_default()
    if secrets.get_secret("ANTHROPIC_API_KEY"):
        return ("anthropic", "claude-sonnet-4-6")
    if secrets.get_secret("OPENROUTER_API_KEY"):
        return ("openrouter", "anthropic/claude-sonnet-4-6")
    return None


def _models(provider: str, model: str) -> dict[str, dict[str, Any]]:
    return {
        "agent": {"provider": provider, "name": model, "temperature": 0.0, "max_tokens": 4096},
        "user": {"provider": provider, "name": model, "temperature": 0.2},
    }


def _run_trial_through_stack(handle: StackHandle, task_path: Path) -> TrialResult:
    """Drive one real trial to a ``TrialResult`` through the composed runner.

    Skips unless the stack is local-mode and a provider key is available. Copies
    the task pack into the runner so the wire task's relative file assets resolve,
    runs ``tolokaforge run-trial`` with ``EXECUTOR_ADDRESS`` pointed at the runner's
    own gRPC server, and returns the single ``result`` wire line's ``TrialResult``.
    """
    if handle.mode != "local":
        pytest.skip("the paid trial runs against the local-mode stack")
    provider = _pick_provider()
    if provider is None:
        pytest.skip("needs an ANTHROPIC or OPENROUTER key")

    from tolokaforge.adapters._task_loader import load_task_yaml
    from tolokaforge.core.trial import TrialResult

    task, task_dir = load_task_yaml(task_path)
    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": _models(*provider),
        "runtime": "shared",
        "conductor": "in_process",
    }

    container_task_dir = f"/tmp/{task_dir.name}"
    copied = compose(handle.project, ["cp", str(task_dir), "runner:/tmp/"], handle.tag)
    assert copied.returncode == 0, f"copying the task pack into the runner failed: {copied.stderr}"

    proc = compose(
        handle.project,
        [
            "exec",
            "-T",
            "-e",
            f"EXECUTOR_ADDRESS={_RUNNER_ADDR}",
            "-w",
            container_task_dir,
            "runner",
            "tolokaforge",
            "run-trial",
        ],
        handle.tag,
        input_text=json.dumps(start) + "\n",
    )
    assert proc.returncode == 0, (
        f"run-trial exec failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one wire line, got: {proc.stdout!r}"
    envelope = json.loads(lines[0])
    assert envelope["type"] == "result", f"trial did not reach a result: {envelope!r}"
    return TrialResult.model_validate(envelope["result"])


@pytest.mark.requires_api
@pytest.mark.llm
def test_bundled_trial_through_composed_stack(composed_stack: StackHandle) -> None:
    """One real bundled db-service trial drives to a graded ``TrialResult``.

    The ``tool_use`` pack exercises db_query/db_update plus the filesystem tools
    over the ``run-trial`` exec wire, proving db-service participates in a real
    graded trial through the composed stack.
    """
    result = _run_trial_through_stack(composed_stack, _PAID_TASK)
    assert result.trajectory.status.value == "completed", result.trajectory.status
    assert result.trajectory.grade is not None, "trial produced no grade"


@pytest.mark.requires_api
@pytest.mark.llm
def test_mock_web_trial_through_composed_stack(composed_stack: StackHandle) -> None:
    """One real mock-web booking trial drives to a graded pass through the stack.

    The ``mock_web_booking`` pack gives the agent only ``http_request``; it books a
    room on mock-web and reports the site-issued confirmation number. A graded pass
    proves mock-web functionally participated. The assertions then independently
    re-check the round-trip so a grading regression that passes for the wrong reason
    is caught: an assistant ``http_request`` POST to ``mock-web`` must appear in the
    tool calls (not merely the seed-message URL), and the site-issued confirmation
    token — which enters the transcript only via the POST response — must be present.
    """
    result = _run_trial_through_stack(composed_stack, _MOCK_WEB_TASK)
    assert result.trajectory.status.value == "completed", result.trajectory.status
    grade = result.trajectory.grade
    assert grade is not None, "trial produced no grade"
    assert grade.binary_pass is True, f"mock-web trial did not grade as a pass: {grade.reasons}"

    assert _agent_posted_to_mock_web(result.trajectory), (
        "no assistant http_request POST to mock-web in the tool calls — "
        "the booking round-trip did not happen"
    )
    transcript = json.dumps(result.trajectory.model_dump(mode="json"))
    assert "BKSEA12345" in transcript, "confirmation token absent — graded pass may be spurious"
