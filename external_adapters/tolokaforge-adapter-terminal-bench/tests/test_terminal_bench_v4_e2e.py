"""End-to-end acceptance of the separate-verifier (4.0) grading path.

Builds a minimal 4.0 task's agent + verifier images, provisions a trial through
the real runtime backend, and grades it through the real
``RunnerServiceImpl`` path (``materialise_path`` + ``run_test_suite`` in the
verifier container), not a stand-in:

* the oracle writes the declared artifact and scores **1.0** — it is
  re-materialised from the agent into the verifier container and the suite there
  passes;
* a no-op writes nothing and scores **0.0** — the artifact is absent, so the
  verifier runs without it and the suite fails.

Heavy (it builds Docker images and brings up compose stacks), so it carries the
``integration`` / ``docker`` / ``requires_docker`` markers plus a
daemon-available skip and runs only when those markers are selected — never in a
plain unit run.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.docker.image import ImageError
from tolokaforge.docker.stacks.core import core_stack

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TASK_ID = "v4_echo"
_RUN_ID = "test-tb-v4-e2e"
_PREBUILD_TIMEOUT_S = 600

_ALIASED_ENGINE_SERVICES: tuple[tuple[str, str], ...] = (
    ("runner", "tolokaforge-runner"),
    ("db-service", "tolokaforge-db-service"),
)


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


@pytest.fixture(scope="module")
def engine_images_with_docker_cli() -> None:
    """Build ``tolokaforge-runner`` with the docker CLI baked in and alias both
    engine images as ``:local`` — the run-path preparation the synthesised task
    compose file references (the runner-side bash tool ``docker exec``s into the
    sibling agent/verifier containers via the mounted socket)."""
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
def adapter(
    tmp_path_factory: pytest.TempPathFactory,
    engine_images_with_docker_cli: None,
) -> TerminalBenchAdapter:
    del engine_images_with_docker_cli  # fixture ordering only
    staging_root = tmp_path_factory.mktemp("tbench-v4-staging")
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(_FIXTURES),
            "task_ids": [_TASK_ID],
            "staging_root": str(staging_root),
        }
    )


@pytest.fixture(scope="module")
def prebuilt_environment(adapter: TerminalBenchAdapter) -> dict[str, Any]:
    """Materialise staging and run the adapter-declared compose builds once.

    A separate-verifier task declares two image builds — the agent and the
    verifier — so this also proves ``docker_stack_requirements()`` emits the
    verifier build and that both Dockerfiles build against a real daemon.
    """
    env = adapter._environment(_TASK_ID)
    task = adapter.to_task_description(_TASK_ID)
    requirements = adapter.docker_stack_requirements()
    built_services = {build.service for build in requirements.image_builds}
    assert built_services == {env.agent_service, env.verifier_service}, built_services
    for build in requirements.image_builds:
        subprocess.run(
            ["docker", "compose", "-f", str(build.compose_file), "build", build.service],
            check=True,
            timeout=_PREBUILD_TIMEOUT_S,
        )
    return {"env": env, "task": task}


@pytest.fixture(scope="module")
def connected_backend(
    prebuilt_environment: dict[str, Any],
) -> Iterator[SharedStackRuntimeBackend]:
    """A connected run-scope backend shared by both grading cases.

    ``connect`` brings the run-scope engine stack (runner + db-service) up once;
    each case provisions and tears down its own trial-scope stack on it.
    """
    task = prebuilt_environment["task"]
    backend = SharedStackRuntimeBackend(
        env_manifest=task.environment_manifest,
        run_id=_RUN_ID,
        mount_docker_socket=True,
    )
    backend.connect()
    try:
        yield backend
    finally:
        backend.close()


def _make_trial_spec(task_description: Any, trial_id: str) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id=_RUN_ID,
        task=task_description,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:8000",
            runner_url="http://placeholder:50051",
        ),
    )


def _provision_and_grade(
    backend: SharedStackRuntimeBackend,
    task: Any,
    trial_id: str,
    *,
    solve: bool,
) -> float:
    """Provision one trial, optionally apply the oracle, grade it, tear it down."""
    spec = _make_trial_spec(task, trial_id)
    handle = backend.provision(spec)
    try:
        register = backend.register_trial(
            trial_id=spec.trial_id,
            trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
        )
        assert register["success"] is True, register.get("error")
        if solve:
            # Apply the oracle in the AGENT container; grading then re-materialises
            # /app/answer.txt into the verifier container before running the suite.
            result = backend.execute_tool(
                trial_id=spec.trial_id,
                tool_name="bash",
                arguments={"command": "echo 42 > /app/answer.txt"},
                call_id="oracle",
            )
            assert result.success is True, result.error
        graded = backend.grade_trial(trial_id=spec.trial_id, llm_messages_json=json.dumps([]))
        assert graded["success"] is True, graded.get("error")
        grade = graded["grade"]
        assert grade is not None
        return grade["score"]
    finally:
        backend.cleanup_trial(trial_id=spec.trial_id)
        backend.teardown(handle)


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestTerminalBenchV4SeparateVerifier:
    """Oracle and no-op acceptance for a separate-verifier (4.0) task."""

    def test_oracle_solution_scores_one(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        score = _provision_and_grade(
            connected_backend,
            prebuilt_environment["task"],
            f"{_TASK_ID}:oracle",
            solve=True,
        )
        assert score == 1.0, (
            f"the oracle writes the declared artifact, which is re-materialised into "
            f"the verifier container where the suite passes; expected 1.0, got {score}."
        )

    def test_noop_scores_zero(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        score = _provision_and_grade(
            connected_backend,
            prebuilt_environment["task"],
            f"{_TASK_ID}:noop",
            solve=False,
        )
        assert score == 0.0, (
            f"a no-op leaves the declared artifact absent, so the verifier runs "
            f"without it and the suite fails; expected 0.0, got {score}."
        )
