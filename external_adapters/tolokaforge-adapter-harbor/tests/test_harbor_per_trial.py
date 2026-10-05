"""End-to-end lock: a Harbor task graded on tolokaforge's runner.

The vendored ``write-release-note`` task is a single-container Harbor task, so
it resolves to a MULTI_SCOPE composition plan: a run-scope ``engine`` stack
that owns the runner + db-service, plus a trial-scope ``task`` stack. That is
the shape the orchestrator routes through ``SharedStackRuntimeBackend`` in
env_manifest mode, so these brackets construct it directly
(``PerTrialRuntimeBackend`` cannot host a plan with a run-scope stack). No LLM
key is required: the trial body is driven by the bash tool directly.

The sequence mirrors the terminal-bench per-trial bracket:

1. build the engine images with the docker CLI baked in and alias them as
   ``tolokaforge-runner:local`` + ``tolokaforge-db-service:local`` — the pair
   the orchestrator prepares on the run path for a docker-CLI adapter;
2. perform the adapter-declared ``docker compose build`` for the task image;
3. ``connect`` → ``provision`` → ``register_trial`` → ``execute_tool`` asserting
   the Harbor layout (``/tests/test.sh``, ``/logs/verifier``, ``/logs/agent``)
   is present inside the container the runner execs into;
4. grade the untouched baseline (reward 0.0 — the release note is absent), then
   run the oracle command through the bash tool and grade again (reward 1.0 —
   the pack's own verifier wrote the reward);
5. ``teardown`` then ``close``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter

from tolokaforge.core.composition_runtime import ComposedEnvHandle
from tolokaforge.core.docker_compose_materialiser import _DockerComposeStackHandle
from tolokaforge.core.execution_mode import select_execution_mode
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.docker.image import ImageError
from tolokaforge.docker.stacks.core import core_stack

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]


def is_docker_daemon_available() -> bool:
    """Docker daemon reachable and operational (ping + credential-store access)."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        docker.auth.load_config().get_all_credentials()
        return True
    except Exception:
        return False


_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES_ROOT = _REPO_ROOT / "examples" / "harbor"
_TASK_ID = "write-release-note"
_RUN_ID = "test-harbor-per-trial"
_PREBUILD_TIMEOUT_S = 900

_ALIASED_ENGINE_SERVICES: tuple[tuple[str, str], ...] = (
    ("runner", "tolokaforge-runner"),
    ("db-service", "tolokaforge-db-service"),
)


@pytest.fixture(scope="module")
def engine_images_with_docker_cli() -> None:
    """Build the runner with ``INSTALL_DOCKER_CLI=true`` and alias both engine
    images as ``:local`` — the run-path preparation for a docker-CLI adapter."""
    stack = core_stack(enable_docker_cli=True)
    stack.build_and_prepare()
    for service_name, alias_repository in _ALIASED_ENGINE_SERVICES:
        image = stack.get_image(service_name)
        assert image is not None, f"engine image {service_name!r} did not build"
        try:
            image.add_alias_tag(alias_repository, "local")
        except ImageError as exc:
            pytest.fail(f"could not alias {service_name!r} as {alias_repository}:local: {exc}")


@pytest.fixture(scope="module")
def adapter(tmp_path_factory: pytest.TempPathFactory) -> HarborAdapter:
    staging_root = tmp_path_factory.mktemp("harbor-staging")
    return HarborAdapter(
        {
            "harbor_tasks_dir": str(_EXAMPLES_ROOT),
            "task_ids": [_TASK_ID],
            "staging_root": str(staging_root),
        }
    )


@pytest.fixture(scope="module")
def prebuilt_environment(
    adapter: HarborAdapter,
    engine_images_with_docker_cli: None,
) -> dict[str, Any]:
    """Materialise the staging dir and run the declared compose build once."""
    del engine_images_with_docker_cli  # fixture ordering only
    env = adapter._environment(_TASK_ID)
    task = adapter.to_task_description(_TASK_ID)
    requirements = adapter.docker_stack_requirements()
    assert len(requirements.image_builds) == 1
    build = requirements.image_builds[0]
    subprocess.run(
        ["docker", "compose", "-f", str(build.compose_file), "build", build.service],
        check=True,
        timeout=_PREBUILD_TIMEOUT_S,
    )
    return {"env": env, "task": task}


def _stack_handle(env_handle: Any) -> _DockerComposeStackHandle:
    assert isinstance(env_handle, ComposedEnvHandle)
    stack_handle = env_handle.trial_stack_handles[0]
    assert isinstance(stack_handle, _DockerComposeStackHandle)
    return stack_handle


def _make_trial_spec(task_description: Any, trial_id: str) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id=_RUN_ID,
        task=task_description,
        execution_mode=select_execution_mode(task_description.metadata),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:8000",
            runner_url="http://placeholder:50051",
        ),
    )


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestHarborPerTrialBracket:
    """The full ``write-release-note`` bracket against a real daemon."""

    def test_oracle_trial_grades_a_reward(self, prebuilt_environment: dict[str, Any]) -> None:
        task = prebuilt_environment["task"]
        backend = SharedStackRuntimeBackend(
            env_manifest=task.environment_manifest,
            run_id=_RUN_ID,
            mount_docker_socket=True,
        )
        spec = _make_trial_spec(task, f"{_TASK_ID}:0")
        stack: _DockerComposeStackHandle | None = None
        container_ids_at_provision: list[str] = []
        try:
            backend.connect()
            handle = backend.provision(spec)
            stack = _stack_handle(handle)
            container_ids_at_provision = [c.ID for c in stack.compose.get_containers() if c.ID]
            assert container_ids_at_provision, "compose stack came up with no containers"

            endpoints = backend.endpoints(handle)
            assert endpoints.runner_url.startswith("http://")

            register = backend.register_trial(
                trial_id=spec.trial_id,
                trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
            )
            assert register["success"] is True, register.get("error")

            # The Harbor layout the verifier and the agent read is present.
            probe = backend.execute_tool(
                trial_id=spec.trial_id,
                tool_name="bash",
                arguments={
                    "command": (
                        "test -f /tests/test.sh && "
                        "test -d /logs/verifier && "
                        "test -d /logs/agent && "
                        "echo READY"
                    ),
                },
                call_id="probe-1",
            )
            assert probe.success is True, probe.error
            assert "READY" in probe.output, probe.output

            # Baseline: the release note is absent, so the pack's verifier scores 0.
            baseline = backend.grade_trial(
                trial_id=spec.trial_id,
                llm_messages_json=json.dumps([]),
            )
            assert baseline["success"] is True, baseline.get("error")
            assert baseline["grade"]["score"] == 0.0, baseline["grade"].get("reasons")

            # Oracle: write exactly the file the instruction asks for.
            solve = backend.execute_tool(
                trial_id=spec.trial_id,
                tool_name="bash",
                arguments={
                    "command": "printf 'harbor release: ready\\n' > /app/RELEASE_NOTE.txt",
                },
                call_id="solve-1",
            )
            assert solve.success is True, solve.error

            solved = backend.grade_trial(
                trial_id=spec.trial_id,
                llm_messages_json=json.dumps([]),
            )
            assert solved["success"] is True, solved.get("error")
            assert solved["grade"]["score"] == 1.0, (
                f"the oracle wrote the required release note, so the Harbor verifier "
                f"must score 1.0; got {solved['grade']['score']}. "
                f"reasons: {solved['grade'].get('reasons')}"
            )

            backend.cleanup_trial(trial_id=spec.trial_id)
            backend.teardown(handle)
        finally:
            backend.close()

        assert stack is not None
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--no-trunc"],
            capture_output=True,
            text=True,
            check=True,
        )
        remaining = set(listed.stdout.split())
        leftover = remaining.intersection(container_ids_at_provision)
        assert not leftover, f"containers survived teardown: {sorted(leftover)!r}"
