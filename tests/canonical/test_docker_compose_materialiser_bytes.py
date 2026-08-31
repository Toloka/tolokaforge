"""Byte-parity between :class:`DockerComposeMaterialiser` and today's inline flows.

The materialiser extracts the compose-file transform sequence today's
``SharedStackRuntimeBackend._materialise_manifest`` (Flow A) and
``PerTrialRuntimeBackend.provision`` (Flow B) drive inline. This test
runs both paths in-process with a stubbed
``testcontainers.compose.DockerCompose`` factory and asserts:

- the transformed compose file bytes on disk are identical (byte-parity
  of the network-policy, credential-injection, socket-mount, and
  ``.env``-write steps),
- the sequence of driver-side calls the stub records is identical (locks
  the invocation order of the extraction so a stage-4 reorder cannot
  silently change lifecycle semantics).

Stage 5's parity contract test extends this by monkey-patching the same
stub factory into
:mod:`tolokaforge.core.shared_stack_runtime` and
:mod:`tolokaforge.core.per_trial_runtime`, so any drift between the
materialiser and the two backends surfaces there too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._docker_compose_stubs import InertDockerCompose, driver_state
from tolokaforge.core.compose_materialisation import (
    apply_network_policy_to_compose_file,
    copy_compose_context,
    inject_runner_credentials,
    make_project_temp_dir,
    mount_docker_socket_into_runner,
    write_compose_env_file,
)
from tolokaforge.core.composition_runtime import (
    MaterialiseContext,
    WriteComposeEnv,
)
from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser
from tolokaforge.core.run_display_events import _NULL_EVENTS
from tolokaforge.core.trial import NetworkPolicy
from tolokaforge.runner.models import StackDecl

pytestmark = pytest.mark.canonical


_FIXTURE = Path(__file__).parent / "fixtures" / "environment_manifest" / "safe_two_service.yaml"
_FIXTURE_RUNNER_SERVICE = "default"


def _run_baseline_flow_a(dest_dir: Path) -> tuple[bytes, InertDockerCompose]:
    """In-process reproduction of today's Flow A transform sequence.

    Mirrors ``SharedStackRuntimeBackend._materialise_manifest`` line-for-line
    for the on-disk transforms + the ``DockerCompose`` driver call.
    """
    copy_compose_context(_FIXTURE, dest_dir)
    compose_file = dest_dir / _FIXTURE.name
    apply_network_policy_to_compose_file(
        compose_file,
        NetworkPolicy.NO_INTERNET,
        _FIXTURE_RUNNER_SERVICE,
        [],
        restricted_services=frozenset(),
    )
    inject_runner_credentials(compose_file, _FIXTURE_RUNNER_SERVICE)
    stub = InertDockerCompose(
        context=str(dest_dir),
        compose_file_name=_FIXTURE.name,
        pull=False,
        build=False,
        wait=True,
    )
    stub.start()
    # LogRouter attach loop in today's _materialise_manifest reads
    # compose.get_containers(); mirror that here so the driver call
    # sequence matches the materialiser's.
    stub.get_containers()
    return compose_file.read_bytes(), stub


def _run_baseline_flow_b(dest_dir: Path, trial_id: str) -> tuple[bytes, InertDockerCompose]:
    """In-process reproduction of today's Flow B transform sequence.

    Mirrors ``PerTrialRuntimeBackend.provision`` line-for-line for the
    on-disk transforms + the ``DockerCompose`` driver call.
    """
    copy_compose_context(_FIXTURE, dest_dir)
    write_compose_env_file(dest_dir, trial_id=trial_id, stack_inputs={"DB_NAME": "example"})
    compose_file = dest_dir / _FIXTURE.name
    apply_network_policy_to_compose_file(
        compose_file,
        NetworkPolicy.NO_INTERNET,
        _FIXTURE_RUNNER_SERVICE,
        [],
        restricted_services=frozenset(),
    )
    inject_runner_credentials(compose_file, _FIXTURE_RUNNER_SERVICE)
    mount_docker_socket_into_runner(compose_file, _FIXTURE_RUNNER_SERVICE)
    stub = InertDockerCompose(
        context=str(dest_dir),
        compose_file_name=_FIXTURE.name,
        pull=False,
        build=False,
        wait=True,
    )
    stub.start()
    # _attach_log_routers reads compose.get_containers(); mirror it.
    stub.get_containers()
    return compose_file.read_bytes(), stub


def _load_env(dest_dir: Path) -> str:
    return (dest_dir / ".env").read_text()


def test_single_run_bytes_parity(tmp_path: Path) -> None:
    """SINGLE_RUN plan: materialiser output matches today's shared-stack
    inline transform sequence byte-for-byte, and the driver-side call
    sequence matches too."""
    baseline_dir = make_project_temp_dir("run-baseline")
    try:
        baseline_bytes, baseline_stub = _run_baseline_flow_a(baseline_dir)

        # Path 2: run the materialiser with the same inert stub factory.
        materialiser_stubs: list[InertDockerCompose] = []

        def factory(**kwargs: Any) -> InertDockerCompose:
            stub = InertDockerCompose(**kwargs)
            materialiser_stubs.append(stub)
            return stub

        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)
        decl = StackDecl(
            stack_id="default",
            compose_file=_FIXTURE,
            stack_scope="run",
            runner_service=_FIXTURE_RUNNER_SERVICE,
        )
        ctx = MaterialiseContext(
            scope_key="run-materialiser",
            stack_id="default",
            network_policy=NetworkPolicy.NO_INTERNET,
            limited_internet_allowlist=(),
            restricted_services=frozenset(),
            mount_docker_socket=False,
            log_capture=None,
            write_compose_env=None,
            events=_NULL_EVENTS,
            component_id_prefix="engine",
        )
        handle = materialiser.materialise(decl, ctx)

        try:
            materialiser_bytes = (Path(handle.temp_dir) / _FIXTURE.name).read_bytes()  # type: ignore[attr-defined]
            assert materialiser_bytes == baseline_bytes
            assert driver_state(materialiser_stubs[0]) == driver_state(baseline_stub)
        finally:
            materialiser.teardown(handle)
    finally:
        import shutil

        shutil.rmtree(baseline_dir, ignore_errors=True)


def test_trial_scoped_bytes_parity(tmp_path: Path) -> None:
    """TRIAL_SCOPED_ONLY plan: materialiser output matches today's
    per-trial inline transform sequence byte-for-byte, including the
    ``.env`` file the trial scope writes."""
    baseline_dir = make_project_temp_dir("trial-baseline")
    try:
        baseline_bytes, baseline_stub = _run_baseline_flow_b(baseline_dir, trial_id="task_a:0")
        baseline_env = _load_env(baseline_dir)

        materialiser_stubs: list[InertDockerCompose] = []

        def factory(**kwargs: Any) -> InertDockerCompose:
            stub = InertDockerCompose(**kwargs)
            materialiser_stubs.append(stub)
            return stub

        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)
        decl = StackDecl(
            stack_id="default",
            compose_file=_FIXTURE,
            stack_scope="trial",
            runner_service=_FIXTURE_RUNNER_SERVICE,
        )
        ctx = MaterialiseContext(
            scope_key="task_a:0",
            stack_id="default",
            network_policy=NetworkPolicy.NO_INTERNET,
            limited_internet_allowlist=(),
            restricted_services=frozenset(),
            mount_docker_socket=True,
            log_capture=None,
            write_compose_env=WriteComposeEnv(
                trial_id="task_a:0", stack_inputs={"DB_NAME": "example"}
            ),
            events=_NULL_EVENTS,
            component_id_prefix="trial/task_a:0",
        )
        handle = materialiser.materialise(decl, ctx)

        try:
            materialiser_bytes = (Path(handle.temp_dir) / _FIXTURE.name).read_bytes()  # type: ignore[attr-defined]
            materialiser_env = (Path(handle.temp_dir) / ".env").read_text()  # type: ignore[attr-defined]
            assert materialiser_bytes == baseline_bytes
            assert materialiser_env == baseline_env
            assert driver_state(materialiser_stubs[0]) == driver_state(baseline_stub)
        finally:
            materialiser.teardown(handle)
    finally:
        import shutil

        shutil.rmtree(baseline_dir, ignore_errors=True)


def test_make_project_temp_dir_default_stack_id_preserves_basename() -> None:
    """The ``stack_id=None`` and ``stack_id="default"`` cases produce the
    pre-composition basename shape — one slug segment. Locks the INV-10
    extension's backward-compat contract."""
    default_dir = make_project_temp_dir("run-x")
    named_default_dir = make_project_temp_dir("run-x", "default")
    two_stack_dir = make_project_temp_dir("run-x", "app")
    try:
        assert default_dir.name.startswith("tolokaforge-run-x-")
        assert named_default_dir.name.startswith("tolokaforge-run-x-")
        assert two_stack_dir.name.startswith("tolokaforge-run-x-app-")
        # And the single-segment shape excludes an extra "app-" prefix segment.
        assert "-app-" not in default_dir.name
        assert "-app-" not in named_default_dir.name
    finally:
        import shutil

        for d in (default_dir, named_default_dir, two_stack_dir):
            shutil.rmtree(d, ignore_errors=True)


def test_make_project_temp_dir_stack_id_slug_sanitised() -> None:
    """Non-alphanumeric characters in ``stack_id`` are sanitised via
    ``compose_trial_slug`` so the resulting basename is still a valid
    compose project name."""
    d = make_project_temp_dir("run-x", "my:stack/with-mix")
    try:
        # ``compose_trial_slug`` maps ``/`` and ``:`` to ``_``.
        assert "my_stack_with-mix" in d.name
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
