"""End-to-end lock: ``fix-billing-holds`` under the per-trial substrate.

The bracket the orchestrator would run for one terminal-bench trial,
driven directly against ``PerTrialRuntimeBackend`` so no LLM key is
required:

1. materialise the adapter's staging directory and resolve the manifest;
2. build the engine images with the docker CLI baked in and alias them
   as ``tolokaforge-runner:local`` + ``tolokaforge-db-service:local`` —
   the pair of steps ``Orchestrator._construct_runtime_backend`` and
   ``Orchestrator._ensure_engine_image_local_aliases`` run on the run
   path when the adapter type is ``terminal_bench``. Then perform the
   adapter-declared ``docker compose build`` for the agent image, the
   step ``Orchestrator._perform_declared_compose_image_builds`` runs
   next. The test drives the substrate directly rather than
   ``Orchestrator.run()``, so it performs these steps itself;
3. ``provision`` → ``endpoints`` → ``register_trial`` → ``execute_tool``
   asserting ``/tests/test.sh``, ``/logs/verifier`` and ``/logs/agent``
   are present inside the container the runner will exec into;
4. ``grade_trial`` — a real ``bash test.sh`` run against the unsolved
   baseline: some tests pass, most fail, so the reward is strictly
   between 0 and 1;
5. ``teardown`` — the compose project's containers are gone.

The concurrency case provisions the same task twice. Because the
per-task agent image build ran once in the module-level fixture, this
exercises per-trial isolation rather than racing two builds of the same
tag.

``TestTerminalBenchHarnessMode`` covers the same bracket for a trial that
brings its own agent: base image, harness layer, forwarded provider
credentials, one ``docker exec`` of the adapter-built command, grading.
It runs against the cheap ``echo-hello`` fixture with the install script
swapped for a stub, so no vendor CLI is downloaded and no provider key is
needed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.docker.image import ImageError
from tolokaforge.docker.stacks.core import core_stack

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES_ROOT = _REPO_ROOT / "examples" / "terminal_bench"
_TASK_ID = "fix-billing-holds"
_RUN_ID = "test-terminal-bench-per-trial"

# The task's Dockerfile installs Python + PostgreSQL + FastAPI. The
# first-time build routinely runs a few minutes; every subsequent test
# in the file reuses the cached image.
_PREBUILD_TIMEOUT_S = 900

# ``(service_name, alias_repository)`` pairs mirrored from
# ``Orchestrator._PER_TRIAL_ALIASED_SERVICES`` — the two engine images
# the synthesised compose file references by ``:local``.
_ALIASED_ENGINE_SERVICES: tuple[tuple[str, str], ...] = (
    ("runner", "tolokaforge-runner"),
    ("db-service", "tolokaforge-db-service"),
)


@pytest.fixture(scope="module")
def engine_images_with_docker_cli() -> None:
    """Build ``tolokaforge-runner`` with ``INSTALL_DOCKER_CLI=true`` and
    alias both engine images as ``:local``.

    Mirrors the run-path preparation: the orchestrator sets
    ``enable_docker_cli=True`` for terminal-bench (see
    ``_run_needs_docker_cli``), calls ``core_stack(...).build_and_prepare()``,
    then aliases each freshly-built engine image as ``:local`` so the
    synthesised task compose file can reference stable tags. A plain
    ``make docker-build-core`` produces the aliases but without the
    docker CLI, so the runner-side ``docker exec`` in the bash tool
    would fail — this fixture is the piece the run path adds on top.
    """
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
def adapter(tmp_path_factory: pytest.TempPathFactory) -> TerminalBenchAdapter:
    staging_root = tmp_path_factory.mktemp("tbench-staging")
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(_EXAMPLES_ROOT),
            "task_ids": [_TASK_ID],
            "staging_root": str(staging_root),
        }
    )


@pytest.fixture(scope="module")
def prebuilt_environment(
    adapter: TerminalBenchAdapter,
    engine_images_with_docker_cli: None,
) -> dict[str, Any]:
    """Materialise the staging dir and run the declared compose build once.

    The compose build is what ``Orchestrator._perform_declared_compose_image_builds``
    invokes on the run path. Running it here proves the adapter's
    ``docker_stack_requirements()`` declaration is well-formed against a
    real daemon and warms the image cache for the per-trial provisions
    below — matching the production sequence.
    """
    del engine_images_with_docker_cli  # fixture ordering only
    env = adapter._environment(_TASK_ID)
    task = adapter.to_task_description(_TASK_ID)
    requirements = adapter.docker_stack_requirements()
    assert len(requirements.image_builds) == 1
    build = requirements.image_builds[0]
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(build.compose_file),
            "build",
            build.service,
        ],
        check=True,
        timeout=_PREBUILD_TIMEOUT_S,
    )
    return {"env": env, "task": task}


def _make_trial_spec(task_description: Any, trial_id: str) -> TrialSpec:
    """Build a ``TrialSpec`` for the fix-billing-holds task.

    ``env_endpoints`` is populated with placeholder URLs — the real
    endpoints for the per-trial runner live inside the trial's own
    compose stack and are resolved by
    :meth:`PerTrialRuntimeBackend.endpoints` at provision time. The spec
    carries only what the runner reads at ``RegisterTrial``.
    """
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


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestTerminalBenchPerTrialBracket:
    """The full ``fix-billing-holds`` bracket against a real daemon."""

    def test_bracket_runs_end_to_end(self, prebuilt_environment: dict[str, Any]) -> None:
        # ``mount_docker_socket=True`` mirrors what
        # ``Orchestrator._construct_runtime_backend`` sets for
        # terminal-bench runs — the runner-side bash tool ``docker exec``s
        # into the sibling agent container via the mounted socket.
        backend = PerTrialRuntimeBackend(mount_docker_socket=True)
        spec = _make_trial_spec(prebuilt_environment["task"], f"{_TASK_ID}:0")
        handle = backend.provision(spec)
        container_ids_at_provision: list[str] = []
        try:
            assert isinstance(handle, _LocalEnvHandle)
            container_ids_at_provision = [c.ID for c in handle.compose.get_containers() if c.ID]
            assert container_ids_at_provision, "compose stack came up with no containers"

            endpoints = backend.endpoints(handle)
            assert endpoints.runner_url.startswith("http://")

            # ``environment_manifest`` describes HOW the orchestrator
            # materialised the substrate — the runner runs inside it and
            # would reject a compose_file path that only exists on the
            # host. Mirrors ``Conductor.register_trial``'s wire exclude.
            register = backend.register_trial(
                trial_id=spec.trial_id,
                trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
            )
            assert register["success"] is True, register.get("error")

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
                timeout_seconds=30.0,
                call_id="probe-1",
            )
            assert probe.success is True, probe.error
            assert "READY" in probe.output, probe.output

            grade_result = backend.grade_trial(
                trial_id=spec.trial_id,
                llm_messages_json=json.dumps([]),
            )
            assert grade_result["success"] is True, grade_result.get("error")
            grade = grade_result["grade"]
            assert grade is not None
            score = grade["score"]
            # Unsolved baseline: some tests pass (health, accessibility),
            # most fail. Range keeps this from breaking when the task's
            # test list changes.
            assert 0.0 < score < 1.0, (
                f"expected 0.0 < score < 1.0 against the unsolved baseline; got {score}. "
                f"reasons: {grade.get('reasons')}"
            )

            backend.cleanup_trial(trial_id=spec.trial_id)
        finally:
            backend.teardown(handle)

        assert not handle.temp_dir.exists()
        # Every container that came up during provision is gone.
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--no-trunc"],
            capture_output=True,
            text=True,
            check=True,
        )
        remaining = set(listed.stdout.split())
        leftover = remaining.intersection(container_ids_at_provision)
        assert not leftover, f"containers survived teardown: {sorted(leftover)!r}"

    def test_concurrent_provisions_produce_isolated_containers(
        self, prebuilt_environment: dict[str, Any]
    ) -> None:
        """Two provisions of the same task get distinct container names.

        The image build ran once in the module-level fixture, so both
        provisions hit the cache — the assertion is on per-trial
        isolation, not on build races.
        """
        backend = PerTrialRuntimeBackend(mount_docker_socket=True)
        spec_a = _make_trial_spec(prebuilt_environment["task"], f"{_TASK_ID}:a")
        spec_b = _make_trial_spec(prebuilt_environment["task"], f"{_TASK_ID}:b")
        handle_a = backend.provision(spec_a)
        try:
            handle_b = backend.provision(spec_b)
            try:
                assert isinstance(handle_a, _LocalEnvHandle)
                assert isinstance(handle_b, _LocalEnvHandle)
                assert handle_a.temp_dir != handle_b.temp_dir
                names_a = {c.Name for c in handle_a.compose.get_containers()}
                names_b = {c.Name for c in handle_b.compose.get_containers()}
                assert names_a.isdisjoint(names_b), (
                    f"expected disjoint container names across concurrent trials; "
                    f"got a={names_a!r}, b={names_b!r}"
                )
            finally:
                backend.teardown(handle_b)
        finally:
            backend.teardown(handle_a)


# ---------------------------------------------------------------------------
# Harness mode — the task brings its own agent
# ---------------------------------------------------------------------------

_HARNESS_TASK_ID = "echo-hello"
_HARNESS = "claude-code"

# A literal fake, asserted by exact value: a "key is set" marker would pass
# even if compose interpolated the wrong one, and the wire is the whole point.
_FAKE_PROVIDER_KEY = "sk-integration-fake"

# Replaces the real install script inside the staging build context. Installs a
# stand-in for the vendor CLI instead of downloading one — the layering, the
# credential wire, and the single-exec trial body are what this test covers,
# and a network install would make it slow and flaky without covering more.
# The stub solves the fixture task, so grading yields a real 1.0 rather than a
# reward the test had to special-case.
_INSTALL_STUB = """#!/bin/sh
set -eu
cat > /usr/local/bin/claude <<'CLI'
#!/bin/sh
echo "harness-stub argv: $*"
echo "harness-stub provider-env: ${ANTHROPIC_API_KEY:-unset}"
printf 'Hello, World!\\n' > /tmp/hello.txt
CLI
chmod +x /usr/local/bin/claude
"""


@pytest.fixture(scope="module")
def harness_adapter(tmp_path_factory: pytest.TempPathFactory) -> TerminalBenchAdapter:
    tasks_dir = _REPO_ROOT / "tests" / "data" / "terminal_bench_tasks"
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(tasks_dir),
            "task_ids": [_HARNESS_TASK_ID],
            "staging_root": str(tmp_path_factory.mktemp("tbench-harness-staging")),
            "agent_harness": _HARNESS,
            "agent_provider_env": {"ANTHROPIC_API_KEY": _FAKE_PROVIDER_KEY},
        }
    )


@pytest.fixture(scope="module")
def prebuilt_harness_environment(
    harness_adapter: TerminalBenchAdapter,
    engine_images_with_docker_cli: None,
) -> dict[str, Any]:
    """Materialise the layered staging dir and run both declared builds in order.

    Mirrors ``Orchestrator._perform_declared_compose_image_builds``, which
    builds ``DockerStackRequirements.image_builds`` in the order the adapter
    declared them — base before layer, since the layer's Dockerfile is ``FROM``
    the base image.
    """
    del engine_images_with_docker_cli  # fixture ordering only
    env = harness_adapter._environment(_HARNESS_TASK_ID)
    task = harness_adapter.to_task_description(_HARNESS_TASK_ID)

    (env.staging_dir / "_harness" / "install-harness.sh").write_text(_INSTALL_STUB)

    requirements = harness_adapter.docker_stack_requirements()
    assert [b.service for b in requirements.image_builds] == ["main-base", "main"]
    for build in requirements.image_builds:
        subprocess.run(
            ["docker", "compose", "-f", str(build.compose_file), "build", build.service],
            check=True,
            timeout=_PREBUILD_TIMEOUT_S,
        )
    return {"env": env, "task": task}


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestTerminalBenchHarnessMode:
    """The harness-mode bracket end-to-end against a real daemon."""

    def test_layered_image_carries_the_cli_and_the_task_stack(
        self, prebuilt_harness_environment: dict[str, Any]
    ) -> None:
        """One exec of the adapter-built command solves the task and grades 1.0.

        The assertions walk the chain the layering exists to support: the CLI
        is on ``PATH`` (harness layer built and installed), the task's own
        tooling survived underneath it (the layer did not replace the base),
        the forwarded credential reached the container's environment, and the
        command the adapter published is what actually ran.
        """
        backend = PerTrialRuntimeBackend(mount_docker_socket=True)
        task = prebuilt_harness_environment["task"]
        spec = _make_trial_spec(task, f"{_HARNESS_TASK_ID}:0")
        handle = backend.provision(spec)
        try:
            assert isinstance(handle, _LocalEnvHandle)
            register = backend.register_trial(
                trial_id=spec.trial_id,
                trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
            )
            assert register["success"] is True, register.get("error")

            # The task's own base image survived under the harness layer.
            probe = backend.execute_tool(
                trial_id=spec.trial_id,
                tool_name="bash",
                arguments={"command": "command -v claude && python3 -m pytest --version"},
                timeout_seconds=60.0,
                call_id="probe-cli",
            )
            assert probe.success is True, probe.error
            assert "/usr/local/bin/claude" in probe.output, probe.output

            harness_command = task.metadata["agent_harness_command"]
            invocation = backend.execute_tool(
                trial_id=spec.trial_id,
                tool_name="bash",
                arguments={"command": harness_command},
                timeout_seconds=task.agent_tools[0].timeout_s,
                call_id="harness-1",
            )
            assert invocation.success is True, invocation.error
            assert 'Create a file /tmp/hello.txt containing the text "Hello, World!"' in (
                invocation.output
            ), invocation.output
            assert f"provider-env: {_FAKE_PROVIDER_KEY}" in invocation.output, invocation.output

            grade_result = backend.grade_trial(
                trial_id=spec.trial_id,
                llm_messages_json=json.dumps([]),
            )
            assert grade_result["success"] is True, grade_result.get("error")
            grade = grade_result["grade"]
            assert grade is not None
            assert grade["score"] == 1.0, (
                f"the harness stub solved the task, so the reference suite must pass; "
                f"got {grade['score']}. reasons: {grade.get('reasons')}"
            )

            backend.cleanup_trial(trial_id=spec.trial_id)
        finally:
            backend.teardown(handle)

    def test_provider_credentials_never_enter_the_image_or_compose_file(
        self, prebuilt_harness_environment: dict[str, Any]
    ) -> None:
        """The credential lives only in the per-trial ``.env``.

        Baking it into the compose file or the image would put it in a layer
        that outlives the trial and travels with any pushed tag.
        """
        env = prebuilt_harness_environment["env"]
        assert _FAKE_PROVIDER_KEY not in env.compose_file.read_text()
        assert (
            _FAKE_PROVIDER_KEY
            not in (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        )

        history = subprocess.run(
            [
                "docker",
                "image",
                "history",
                "--no-trunc",
                "--format",
                "{{.CreatedBy}}",
                f"tbench-{_HARNESS_TASK_ID}:local-{_HARNESS}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert _FAKE_PROVIDER_KEY not in history.stdout
