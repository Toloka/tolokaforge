"""A trial's final state reaches the engine host by reference (ADR-0069).

A task whose environment declares ``final_state_delivery: mount`` has the
engine bind-mount a per-trial writable directory into its trial-scope runner.
``GetState`` with ``state_by_reference`` writes the final state there and
answers with the file's path, SHA-256 and size; the host checks the file without
parsing it, records a small ``env.yaml`` that references it, and copies it into
the trial bundle as ``final_state.json``. Every place that would have to hold
the state whole refuses such a task instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._factories import write_yaml_file
from tests.utils.runner_requests import register_request, trial_spec_json
from tests.utils.runner_requests import simple_task_description as simple_task_description_dict
from tolokaforge.core.compose_materialisation import mount_final_state_dir_into_runner
from tolokaforge.core.grading.bundle_producer import serialize_bundle_from_substrate
from tolokaforge.core.models import EnvironmentPatch, StackPatch
from tolokaforge.core.output.artifacts import (
    InMemoryArtifactWriter,
    RedactedBundleError,
    bundle_redaction,
    read_final_env_state,
)
from tolokaforge.core.output_writer import (
    ENV_FILENAME,
    FINAL_STATE_FILENAME,
    FINAL_STATE_REF_KEY,
    OutputWriter,
)
from tolokaforge.core.project_loader import resolve as resolve_environment
from tolokaforge.core.redaction import SensitiveKeyRedaction
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GradeDispatch
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.final_state_file import (
    FINAL_STATE_DIR_ENV_VAR,
    FINAL_STATE_MOUNT_PATH,
    FinalStateFileError,
    StateFile,
    copy_state_file,
    resolve_state_file,
    verify_state_file,
    write_state_file,
)
from tolokaforge.runner.models import (
    EnvironmentManifest,
    RunnerGradingConfig,
    ServiceSpec,
    TaskDescription,
)
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION

pytestmark = pytest.mark.unit


_RUNNER_COMPOSE = {
    "services": {
        "runner": {"image": "tolokaforge-runner:local", "ports": ["50051"]},
        "db-service": {"image": "tolokaforge-db-service:local", "ports": ["8000"]},
    }
}

_STATE = {
    "users": [{"id": "u1", "name": "Ada", "balance": 12.5, "note": "two\nlines"}],
    "orders": [{"id": "o1", "user_id": "u1", "paid": False, "refund": None}],
    "user_db": {"device": {"wifi": True}},
}


def _compose(tmp_path: Path) -> Path:
    compose = tmp_path / "environment.compose.yaml"
    write_yaml_file(compose, _RUNNER_COMPOSE)
    return compose


def _state_file(directory: Path, state: dict[str, Any] | None = None) -> StateFile:
    """The state as the runner writes it, with its path placed on this machine."""
    directory.mkdir(parents=True, exist_ok=True)
    return resolve_state_file(directory, write_state_file(directory, state or _STATE))


# ---------------------------------------------------------------------------
# The state file
# ---------------------------------------------------------------------------


class TestStateFile:
    def test_the_file_holds_the_bytes_an_inline_answer_would_carry(self, tmp_path: Path) -> None:
        state = {**_STATE, "7": {"nested": {1: "int key"}}, "unicode": "Привет"}

        written = write_state_file(tmp_path, state)

        data = (tmp_path / FINAL_STATE_FILENAME).read_bytes()
        assert data == json.dumps(state).encode()
        assert written == StateFile(
            path=FINAL_STATE_FILENAME, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        assert not list(tmp_path.glob(".*partial"))

    def test_an_empty_state_is_an_empty_object(self, tmp_path: Path) -> None:
        write_state_file(tmp_path, {})

        assert (tmp_path / FINAL_STATE_FILENAME).read_text() == "{}"

    def test_a_failed_write_leaves_no_file(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(TypeError):
            write_state_file(out, {"t": object()})

        assert list(out.iterdir()) == []

    @pytest.mark.parametrize("path", ["", "/etc/passwd", "../outside.json", "a/../../b", "a\\b"])
    def test_a_reported_path_that_leaves_the_directory_is_refused(
        self, tmp_path: Path, path: str
    ) -> None:
        with pytest.raises(FinalStateFileError, match="final-state directory"):
            resolve_state_file(tmp_path, StateFile(path=path, sha256="0" * 64, size=0))

    def test_a_symlink_out_of_the_directory_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "out"
        directory.mkdir()
        (tmp_path / "elsewhere.json").write_text("{}")
        (directory / "link.json").symlink_to(tmp_path / "elsewhere.json")

        with pytest.raises(FinalStateFileError, match="resolves outside"):
            resolve_state_file(directory, StateFile(path="link.json", sha256="0" * 64, size=2))

    def test_verification_names_a_missing_file_a_size_and_a_digest_mismatch(
        self, tmp_path: Path
    ) -> None:
        state_file = _state_file(tmp_path / "out")

        verify_state_file(state_file)
        with pytest.raises(FinalStateFileError, match="bytes; the runner reported"):
            verify_state_file(StateFile(state_file.path, state_file.sha256, state_file.size + 1))
        with pytest.raises(FinalStateFileError, match="hashes to"):
            verify_state_file(StateFile(state_file.path, "0" * 64, state_file.size))
        with pytest.raises(FinalStateFileError, match="does not exist"):
            verify_state_file(StateFile(str(tmp_path / "absent"), "0" * 64, 0))

    def test_a_copy_of_changed_bytes_leaves_nothing_at_the_destination(
        self, tmp_path: Path
    ) -> None:
        state_file = _state_file(tmp_path / "out")
        Path(state_file.path).write_text(json.dumps({"tampered": True}))
        destination = tmp_path / "bundle" / FINAL_STATE_FILENAME
        destination.parent.mkdir()

        with pytest.raises(FinalStateFileError, match="changed after it was captured"):
            copy_state_file(state_file, destination)

        assert list(destination.parent.iterdir()) == []


# ---------------------------------------------------------------------------
# The runner answers by reference
# ---------------------------------------------------------------------------


class TestGetStateByReference:
    def _register(self, runner_service: Any, context: Any, trial_id: str) -> None:
        response = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(simple_task_description_dict(), trial_id=trial_id),
                trial_id=trial_id,
            ),
            context,
        )
        assert response.success is True, response.error

    def test_the_runner_writes_the_state_and_answers_with_the_file(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(FINAL_STATE_DIR_ENV_VAR, str(tmp_path))
        trial_id = "by_ref:0"
        self._register(runner_service, mock_grpc_context, trial_id)

        inline = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=True), mock_grpc_context
        )
        by_ref = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=True, state_by_reference=True),
            mock_grpc_context,
        )

        assert by_ref.success is True, by_ref.error
        assert by_ref.state_json == ""
        assert by_ref.stable_hash == inline.stable_hash
        assert by_ref.state_file.path == FINAL_STATE_FILENAME
        data = (tmp_path / FINAL_STATE_FILENAME).read_bytes()
        assert data == inline.state_json.encode()
        assert by_ref.state_file.size == len(data)
        assert by_ref.state_file.sha256 == hashlib.sha256(data).hexdigest()
        assert not inline.HasField("state_file")

    def test_the_stable_state_by_reference_is_the_inline_stable_answer(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(FINAL_STATE_DIR_ENV_VAR, str(tmp_path))
        trial_id = "by_ref:stable"
        self._register(runner_service, mock_grpc_context, trial_id)

        inline = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=False), mock_grpc_context
        )
        by_ref = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=False, state_by_reference=True),
            mock_grpc_context,
        )

        assert by_ref.success is True, by_ref.error
        assert (tmp_path / FINAL_STATE_FILENAME).read_bytes() == inline.state_json.encode()
        assert by_ref.stable_hash == inline.stable_hash
        assert by_ref.full_hash == inline.full_hash == ""

    def test_a_runner_without_the_mount_refuses_the_read(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(FINAL_STATE_DIR_ENV_VAR, raising=False)
        trial_id = "by_ref:1"
        self._register(runner_service, mock_grpc_context, trial_id)

        response = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, state_by_reference=True), mock_grpc_context
        )

        assert response.success is False
        assert FINAL_STATE_DIR_ENV_VAR in response.error


# ---------------------------------------------------------------------------
# The engine's client and backend
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, response: pb2.GetStateResponse) -> None:
        self.response = response
        self.requests: list[pb2.GetStateRequest] = []

    def GetState(self, request: pb2.GetStateRequest) -> pb2.GetStateResponse:  # noqa: N802
        self.requests.append(request)
        return self.response


def _client(response: pb2.GetStateResponse) -> Any:
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient

    client = GrpcRunnerClient(runner_address="localhost:1")
    client.stub = _Stub(response)  # type: ignore[assignment]
    return client


class TestClientAndBackend:
    def test_the_client_asks_by_reference_only_when_told_and_reads_the_file(self) -> None:
        answer = pb2.GetStateResponse(
            success=True,
            state_file=pb2.StateFileRef(path=FINAL_STATE_FILENAME, sha256="a" * 64, size=3),
        )
        client = _client(answer)

        inline = client.get_state("t:0")
        by_ref = client.get_state("t:0", by_reference=True)

        assert [r.state_by_reference for r in client.stub.requests] == [False, True]
        assert "state_file" not in inline
        assert by_ref["state_file"] == StateFile(FINAL_STATE_FILENAME, "a" * 64, 3)

    def _backend(self, final_state_dir: Path | None, answer: dict[str, Any]) -> Any:
        from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

        backend = SharedStackRuntimeBackend.__new__(SharedStackRuntimeBackend)
        backend._env_handles = {"t:0": SimpleNamespace(final_state_dir=final_state_dir)}
        client = MagicMock()
        client.get_state.return_value = answer
        backend._runner_client_for = lambda trial_id: client  # type: ignore[method-assign]
        return backend

    def test_the_backend_places_the_file_under_its_side_of_the_mount(self, tmp_path: Path) -> None:
        reported = StateFile(FINAL_STATE_FILENAME, "a" * 64, 3)
        backend = self._backend(tmp_path, {"success": True, "state_file": reported})

        result = backend.get_state("t:0", by_reference=True)

        assert result["state_file"] == StateFile(
            str((tmp_path / FINAL_STATE_FILENAME).resolve()), "a" * 64, 3
        )

    @pytest.mark.parametrize(
        ("final_state_dir", "answer", "cause"),
        [
            (None, {"success": True}, "without a final-state directory"),
            ("dir", {"success": True, "state_file": None}, "answered without a state file"),
            (
                "dir",
                {"success": True, "state_file": StateFile("../x.json", "a" * 64, 1)},
                "not a relative path",
            ),
        ],
    )
    def test_a_read_the_backend_cannot_place_is_a_failed_read(
        self, tmp_path: Path, final_state_dir: str | None, answer: dict[str, Any], cause: str
    ) -> None:
        backend = self._backend(tmp_path if final_state_dir else None, answer)

        result = backend.get_state("t:0", by_reference=True)

        assert result["success"] is False
        assert cause in result["error"]


# ---------------------------------------------------------------------------
# The environment resolver, the compose mount and the composer
# ---------------------------------------------------------------------------


class TestResolver:
    def test_inline_is_the_default(self, tmp_path: Path) -> None:
        manifest = resolve_environment(
            None,
            EnvironmentPatch(
                stack=StackPatch(compose_file=_compose(tmp_path), runner_service="runner")
            ),
        )

        assert manifest is not None
        assert manifest.final_state_delivery == "inline"

    def test_mount_is_refused_on_a_run_scope_runner(self, tmp_path: Path) -> None:
        shared = {name: ServiceSpec(isolation="shared") for name in _RUNNER_COMPOSE["services"]}
        with pytest.raises(ValueError, match="final_state_delivery: mount needs the runner"):
            resolve_environment(
                None,
                EnvironmentPatch(
                    stack=StackPatch(compose_file=_compose(tmp_path), runner_service="runner"),
                    services=shared,
                    final_state_delivery="mount",
                ),
            )

    def test_a_project_default_survives_the_tasks_own_stack(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        manifest = resolve_environment(
            EnvironmentPatch(
                stack=StackPatch(compose_file=_compose(project_dir), runner_service="runner"),
                final_state_delivery="mount",
            ),
            EnvironmentPatch(
                stack=StackPatch(compose_file=_compose(task_dir), runner_service="runner")
            ),
        )

        assert manifest is not None
        assert manifest.final_state_delivery == "mount"
        assert manifest.tool_artifacts_delivery == "inline"


class TestComposeMount:
    def test_the_directory_is_mounted_writable_and_named_to_the_runner(
        self, tmp_path: Path
    ) -> None:
        compose = _compose(tmp_path)
        out = tmp_path / "out"
        out.mkdir()

        mount_final_state_dir_into_runner(compose, "runner", out)

        services = yaml.safe_load(compose.read_text())["services"]
        assert {
            "type": "bind",
            "source": str(out),
            "target": FINAL_STATE_MOUNT_PATH,
            "read_only": False,
        } in services["runner"]["volumes"]
        env = services["runner"]["environment"]
        assert (
            env[FINAL_STATE_DIR_ENV_VAR] == FINAL_STATE_MOUNT_PATH
            if isinstance(env, dict)
            else f"{FINAL_STATE_DIR_ENV_VAR}={FINAL_STATE_MOUNT_PATH}" in env
        )
        assert "volumes" not in services["db-service"]

    def test_a_service_declaring_the_variable_is_refused(self, tmp_path: Path) -> None:
        compose = tmp_path / "environment.compose.yaml"
        doc = json.loads(json.dumps(_RUNNER_COMPOSE))
        doc["services"]["runner"]["environment"] = [f"{FINAL_STATE_DIR_ENV_VAR}=/elsewhere"]
        write_yaml_file(compose, doc)

        with pytest.raises(ValueError, match="which the engine sets"):
            mount_final_state_dir_into_runner(compose, "runner", tmp_path)

    def test_a_volume_at_the_mount_path_is_refused(self, tmp_path: Path) -> None:
        compose = tmp_path / "environment.compose.yaml"
        doc = json.loads(json.dumps(_RUNNER_COMPOSE))
        doc["services"]["runner"]["volumes"] = [f"./state:{FINAL_STATE_MOUNT_PATH}"]
        write_yaml_file(compose, doc)

        with pytest.raises(ValueError, match="already mounts a volume"):
            mount_final_state_dir_into_runner(compose, "runner", tmp_path)

    def test_a_directory_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not an absolute directory"):
            mount_final_state_dir_into_runner(_compose(tmp_path), "runner", tmp_path / "absent")


def _mount_spec(compose: Path) -> Any:
    from tests.unit.test_default_substrate_composer import _manifest, _trial_spec

    spec = _trial_spec(_manifest(compose))
    manifest = spec.task.environment_manifest.model_copy(update={"final_state_delivery": "mount"})
    spec.task = spec.task.model_copy(update={"environment_manifest": manifest})
    return spec


class TestComposer:
    def _composer(self, materialiser: Any) -> Any:
        from tests.unit.test_default_substrate_composer import (
            _always_ready_loader,
            _fake_client_factory,
        )
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        return DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )

    def test_the_runner_stack_gets_a_fresh_directory_removed_at_teardown(
        self, tmp_path: Path
    ) -> None:
        from tests.unit.test_default_substrate_composer import (
            _decl,
            _empty_run_sub,
            _FakeMaterialiser,
        )

        compose = _compose(tmp_path)
        materialiser = _FakeMaterialiser()
        composer = self._composer(materialiser)
        plan = [
            _decl(compose, stack_id="world", stack_scope="trial", runner_service="runner"),
            _decl(compose, stack_id="side", stack_scope="trial", runner_service=None),
        ]

        handle = composer.provision_trial(plan, _mount_spec(compose), _empty_run_sub())

        runner_ctx, side_ctx = materialiser.materialise_contexts
        assert side_ctx.final_state_dir is None
        assert runner_ctx.final_state_dir == handle.final_state_dir
        assert handle.final_state_dir.is_dir()
        assert handle.final_state_dir.stat().st_mode & 0o777 == 0o777
        composer.teardown_trial(handle)
        assert not handle.final_state_dir.exists()
        assert not handle.final_state_dir.parent.exists()

    def test_an_inline_task_gets_no_directory(self, tmp_path: Path) -> None:
        from tests.unit.test_default_substrate_composer import (
            _decl,
            _empty_run_sub,
            _FakeMaterialiser,
            _manifest,
            _trial_spec,
        )

        compose = _compose(tmp_path)
        materialiser = _FakeMaterialiser()
        plan = [_decl(compose, stack_id="world", stack_scope="trial", runner_service="runner")]

        handle = self._composer(materialiser).provision_trial(
            plan, _trial_spec(_manifest(compose)), _empty_run_sub()
        )

        assert handle.final_state_dir is None
        assert [ctx.final_state_dir for ctx in materialiser.materialise_contexts] == [None]

    def test_a_failed_provision_removes_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_default_substrate_composer import (
            _decl,
            _empty_run_sub,
            _FakeMaterialiser,
        )

        made: list[Path] = []
        import tolokaforge.core.default_substrate_composer as composer_module

        real = composer_module._make_final_state_dir

        def recording(trial_id: str) -> Path:
            made.append(real(trial_id))
            return made[-1]

        monkeypatch.setattr(composer_module, "_make_final_state_dir", recording)
        compose = _compose(tmp_path)
        materialiser = _FakeMaterialiser()
        materialiser.materialise = MagicMock(side_effect=RuntimeError("compose up failed"))
        plan = [_decl(compose, stack_id="world", stack_scope="trial", runner_service="runner")]

        with pytest.raises(RuntimeError, match="compose up failed"):
            self._composer(materialiser).provision_trial(
                plan, _mount_spec(compose), _empty_run_sub()
            )

        assert made and not made[0].parent.exists()

    def test_a_directory_that_cannot_be_removed_is_named_in_the_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import tolokaforge.core.default_substrate_composer as composer_module

        directory = tmp_path / "parent" / "trial"
        monkeypatch.setattr(
            composer_module.shutil, "rmtree", MagicMock(side_effect=PermissionError("in use"))
        )

        with caplog.at_level(logging.ERROR, logger=composer_module.__name__):
            composer_module._remove_final_state_dir(directory)

        assert str(directory) in caplog.text
        assert "in use" in caplog.text

    def test_a_run_scope_runner_is_refused_before_any_stack_starts(self, tmp_path: Path) -> None:
        from tests.unit.test_default_substrate_composer import (
            _decl,
            _empty_run_sub,
            _FakeMaterialiser,
        )

        compose = _compose(tmp_path)
        materialiser = _FakeMaterialiser()
        plan = [_decl(compose, stack_id="shared", stack_scope="run", runner_service="runner")]

        with pytest.raises(ProvisionError, match="final_state_delivery: mount"):
            self._composer(materialiser).provision_trial(
                plan, _mount_spec(compose), _empty_run_sub()
            )
        assert materialiser.materialise_contexts == []


def test_the_docker_materialiser_writes_the_mount_into_the_trial_compose(tmp_path: Path) -> None:
    from tests.canonical._docker_compose_stubs import InertDockerCompose
    from tolokaforge.core.composition_runtime import MaterialiseContext
    from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser
    from tolokaforge.core.run_display_events import _NULL_EVENTS
    from tolokaforge.core.trial import NetworkPolicy
    from tolokaforge.runner.models import StackDecl

    compose = _compose(tmp_path / "stack")
    out = tmp_path / "out"
    out.mkdir()
    materialiser = DockerComposeMaterialiser(docker_compose_factory=InertDockerCompose)
    ctx = MaterialiseContext(
        scope_key="t:0",
        stack_id="world",
        network_policy=NetworkPolicy.NO_INTERNET,
        limited_internet_allowlist=(),
        restricted_services=frozenset(),
        mount_docker_socket=False,
        expose_substrate=False,
        log_capture=None,
        write_compose_env=None,
        events=_NULL_EVENTS,
        component_id_prefix="trial/t:0",
        final_state_dir=out,
    )
    decl = StackDecl(
        stack_id="world", compose_file=compose, stack_scope="trial", runner_service="runner"
    )

    handle = materialiser.materialise(decl, ctx)
    try:
        written = yaml.safe_load((handle.temp_dir / compose.name).read_text())  # type: ignore[attr-defined]
        assert {
            "type": "bind",
            "source": str(out),
            "target": FINAL_STATE_MOUNT_PATH,
            "read_only": False,
        } in written["services"]["runner"]["volumes"]
    finally:
        materialiser.teardown(handle)


# ---------------------------------------------------------------------------
# The conductor's capture
# ---------------------------------------------------------------------------


class TestCapture:
    def _conductor(self, answer: dict[str, Any] | Exception) -> Any:
        from tolokaforge.core.conductor import InProcessConductor

        conductor = InProcessConductor(
            adapter=MagicMock(),
            artifact_writer=MagicMock(),
            config=MagicMock(),
            logger=MagicMock(),
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=Path("/tmp"),
        )
        if isinstance(answer, Exception):
            conductor.runtime_backend.get_state.side_effect = answer
        else:
            conductor.runtime_backend.get_state.return_value = answer
        return conductor

    def _spec(self, tmp_path: Path) -> Any:
        from tests.unit.test_conductor import _make_spec

        spec = _make_spec()
        manifest = EnvironmentManifest(
            compose_file=_compose(tmp_path), runner_service="runner", final_state_delivery="mount"
        )
        initial_state = spec.task.initial_state.model_copy(
            update={"tables": {"users": [{"id": "u1"}]}}
        )
        return spec.model_copy(
            update={
                "task": spec.task.model_copy(
                    update={"environment_manifest": manifest, "initial_state": initial_state}
                )
            }
        )

    def _setup(self) -> MagicMock:
        setup = MagicMock()
        setup.trial_id = "t1:0"
        setup.trial_idx = 0
        setup.env_state.get_final_state_outside_db.return_value = {"filesystem": {"a.txt": "x"}}
        setup.env_state.agent_visible_dir = Path("/work")
        return setup

    def test_the_state_is_a_checked_file_and_env_yaml_only_references_it(
        self, tmp_path: Path
    ) -> None:
        from tests.unit.test_conductor import _default_success_trajectory

        state_file = _state_file(tmp_path / "out")
        conductor = self._conductor({"success": True, "state_file": state_file})
        setup = self._setup()
        trajectory = _default_success_trajectory("t1", 0)

        returned = conductor._capture_final_state(self._spec(tmp_path), setup, trajectory)

        assert returned == state_file
        conductor.runtime_backend.get_state.assert_called_once_with("t1:0", by_reference=True)
        stash = trajectory.final_env_state
        assert stash[FINAL_STATE_REF_KEY] == state_file.as_ref()
        assert not {"agent", "user", "db", "final_state_error"} & stash.keys()
        assert stash["filesystem"] == {"a.txt": "x"}
        assert stash["environment"]["runner_service"] == "runner"
        setup.env_state.get_final_state.assert_not_called()

    @pytest.mark.parametrize(
        ("answer", "cause"),
        [
            ({"success": False, "error": "no mount"}, "no mount"),
            ({"success": True, "state_file": None}, "answered without a file"),
            (ConnectionError("runner gone"), "ConnectionError: runner gone"),
        ],
    )
    def test_a_lost_capture_records_its_cause_and_no_reference(
        self, tmp_path: Path, answer: Any, cause: str
    ) -> None:
        from tests.unit.test_conductor import _default_success_trajectory

        trajectory = _default_success_trajectory("t1", 0)

        returned = self._conductor(answer)._capture_final_state(
            self._spec(tmp_path), self._setup(), trajectory
        )

        assert returned is None
        assert FINAL_STATE_REF_KEY not in trajectory.final_env_state
        assert cause in trajectory.final_env_state["final_state_error"]

    def test_a_file_that_does_not_match_its_answer_is_a_lost_capture(self, tmp_path: Path) -> None:
        from tests.unit.test_conductor import _default_success_trajectory

        state_file = _state_file(tmp_path / "out")
        Path(state_file.path).write_text("{}")
        trajectory = _default_success_trajectory("t1", 0)

        returned = self._conductor(
            {"success": True, "state_file": state_file}
        )._capture_final_state(self._spec(tmp_path), self._setup(), trajectory)

        assert returned is None
        assert "unusable state file" in trajectory.final_env_state["final_state_error"]

    def test_an_inline_task_never_asks_by_reference(self, tmp_path: Path) -> None:
        from tests.unit.test_conductor import _default_success_trajectory

        spec = self._spec(tmp_path)
        manifest = spec.task.environment_manifest.model_copy(
            update={"final_state_delivery": "inline"}
        )
        spec = spec.model_copy(
            update={"task": spec.task.model_copy(update={"environment_manifest": manifest})}
        )
        conductor = self._conductor({"success": True, "state_json": '{"users": []}'})
        setup = self._setup()
        setup.env_state.get_final_state.return_value = {"db": {"users": []}}

        returned = conductor._capture_final_state(spec, setup, _default_success_trajectory("t1", 0))

        assert returned is None
        conductor.runtime_backend.get_state.assert_called_once_with("t1:0")


# ---------------------------------------------------------------------------
# The trial bundle
# ---------------------------------------------------------------------------


def _env_state(state_file: StateFile) -> dict[str, Any]:
    return {
        FINAL_STATE_REF_KEY: state_file.as_ref(),
        "filesystem": {"a.txt": "x"},
        "agent_visible_dir": "/work",
    }


class TestBundle:
    def test_env_yaml_stays_small_and_the_state_is_copied_beside_it(self, tmp_path: Path) -> None:
        state_file = _state_file(tmp_path / "out")
        bundle = tmp_path / "bundle"

        OutputWriter(bundle).write_env_state(_env_state(state_file), state_file)

        env = yaml.safe_load((bundle / ENV_FILENAME).read_text())
        assert env == _env_state(state_file)
        assert (bundle / FINAL_STATE_FILENAME).read_bytes() == Path(state_file.path).read_bytes()

    def test_the_reader_rebuilds_the_inline_views(self, tmp_path: Path) -> None:
        from tolokaforge.core.env_state import final_db_state_views

        state_file = _state_file(tmp_path / "out")
        bundle = tmp_path / "bundle"
        OutputWriter(bundle).write_env_state(_env_state(state_file), state_file)

        env = read_final_env_state(bundle)

        assert {key: env[key] for key in ("agent", "user", "db")} == final_db_state_views(_STATE)
        assert env["user"] == {"device": {"wifi": True}}
        assert env["filesystem"] == {"a.txt": "x"}
        assert FINAL_STATE_REF_KEY not in env

    def test_the_reader_returns_an_inline_env_yaml_as_read(self, tmp_path: Path) -> None:
        OutputWriter(tmp_path).write_env_state({"db": {"users": []}, "agent_visible_dir": "/w"})

        assert read_final_env_state(tmp_path) == {"db": {"users": []}, "agent_visible_dir": "/w"}

    def test_the_reader_refuses_a_sidecar_that_does_not_match(self, tmp_path: Path) -> None:
        state_file = _state_file(tmp_path / "out")
        bundle = tmp_path / "bundle"
        OutputWriter(bundle).write_env_state(_env_state(state_file), state_file)
        (bundle / FINAL_STATE_FILENAME).write_text("{}")

        with pytest.raises(ValueError, match="bytes; the runner reported"):
            read_final_env_state(bundle)

    def test_a_redacting_policy_withholds_the_file_and_says_so(self, tmp_path: Path) -> None:
        state_file = _state_file(tmp_path / "out")
        bundle = tmp_path / "bundle"

        OutputWriter(bundle, redaction=SensitiveKeyRedaction()).write_env_state(
            _env_state(state_file), state_file
        )

        assert not (bundle / FINAL_STATE_FILENAME).exists()
        stamp = bundle_redaction(bundle)
        assert stamp is not None and FINAL_STATE_FILENAME in stamp.omitted
        assert ENV_FILENAME in stamp.artifacts
        with pytest.raises(RedactedBundleError, match="withheld final_state.json"):
            read_final_env_state(bundle)

    def test_a_rewrite_without_a_file_takes_an_older_one_away(self, tmp_path: Path) -> None:
        state_file = _state_file(tmp_path / "out")
        writer = OutputWriter(tmp_path / "bundle")
        writer.write_env_state(_env_state(state_file), state_file)

        writer.write_env_state({"final_state_error": "lost", "agent_visible_dir": "/w"})

        assert not (tmp_path / "bundle" / FINAL_STATE_FILENAME).exists()

    def test_a_redacted_rewrite_without_a_file_drops_the_withholding_from_the_stamp(
        self, tmp_path: Path
    ) -> None:
        state_file = _state_file(tmp_path / "out")
        writer = OutputWriter(tmp_path / "bundle", redaction=SensitiveKeyRedaction())
        writer.write_env_state(_env_state(state_file), state_file)

        writer.write_env_state({"final_state_error": "lost", "agent_visible_dir": "/w"})

        stamp = bundle_redaction(tmp_path / "bundle")
        assert stamp is not None and FINAL_STATE_FILENAME not in stamp.omitted

    def test_the_in_memory_writer_records_the_reference(self, tmp_path: Path) -> None:
        from tests.unit.test_conductor import _default_success_trajectory

        state_file = _state_file(tmp_path / "out")
        writer = InMemoryArtifactWriter()

        writer.write_trial_bundle(
            tmp_path,
            _default_success_trajectory("t1", 0),
            {},
            _env_state(state_file),
            MagicMock(),
            final_state_file=state_file,
        )

        assert writer.trials[tmp_path].final_state_file == state_file


# ---------------------------------------------------------------------------
# Graders that would read the state whole refuse the task
# ---------------------------------------------------------------------------


def _mount_description(tmp_path: Path) -> TaskDescription:
    return TaskDescription(
        task_id="mount_task",
        name="mount task",
        category="test",
        description="a task delivering its final state by reference",
        adapter_type="native",
        system_prompt="",
        environment_manifest=EnvironmentManifest(
            compose_file=_compose(tmp_path), runner_service="runner", final_state_delivery="mount"
        ),
    )


class TestGradersThatReadTheStateWhole:
    def test_the_detached_grader_refuses(self, tmp_path: Path) -> None:
        grading = RunnerGradingConfig(
            combine_method="weighted", weights={"state_checks": 1.0}, pass_threshold=0.5
        )
        dispatch = GradeDispatch(
            trial_id="mount_task:0",
            llm_messages_json="[]",
            termination_reason="",
            task_config_json=grading.model_dump_json(),
            judge_model_config_json="",
            task_description_json=_mount_description(tmp_path).model_dump_json(),
            runner_substrate_address="localhost:1",
        )

        with pytest.raises(GradingFailedError, match="final_state_delivery"):
            GraderCompositeDispatch(logging.getLogger("mount")).grade(dispatch)  # type: ignore[arg-type]

    def test_the_snapshot_bundle_producer_refuses(self, tmp_path: Path) -> None:
        class _Substrate:
            def filesystem_root(self) -> Path:
                return tmp_path

        with pytest.raises(ValueError, match="embeds the whole final state"):
            serialize_bundle_from_substrate(
                substrate=_Substrate(),
                trial_id="mount_task:0",
                out_dir=tmp_path / "bundle",
                trajectory=None,
                task_description=_mount_description(tmp_path),
            )


class TestRunStartGate:
    def _orchestrator(self, tmp_path: Path, grader: dict[str, Any] | None) -> Any:
        description = _mount_description(tmp_path)
        grader_config = None
        if grader is not None:
            grader_config = SimpleNamespace(
                name=grader.get("name"),
                snapshot=SimpleNamespace(enabled=grader.get("snapshot", False)),
            )
        return SimpleNamespace(
            config=SimpleNamespace(grader=grader_config),
            adapter=SimpleNamespace(trial_grader_name="runner_rpc"),
            tasks=[SimpleNamespace(task_id="mount_task")],
            _task_description=lambda task_id: description,
        )

    @pytest.mark.parametrize(
        ("grader", "fragment"),
        [
            ({"name": "grader_rpc"}, "grader 'grader_rpc'"),
            ({"name": "queue"}, "grader 'queue'"),
            ({"name": "runner_rpc", "snapshot": True}, "grader.snapshot.enabled=true"),
        ],
    )
    def test_a_grader_that_reads_the_state_whole_is_refused(
        self, tmp_path: Path, grader: dict[str, Any], fragment: str
    ) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        with pytest.raises(ValueError, match=fragment) as excinfo:
            Orchestrator._refuse_final_state_mount_the_grader_cannot_read(
                self._orchestrator(tmp_path, grader)
            )
        assert "mount_task" in str(excinfo.value)

    @pytest.mark.parametrize("grader", [None, {"name": "runner_rpc"}])
    def test_the_runner_grader_passes(self, tmp_path: Path, grader: dict[str, Any] | None) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        Orchestrator._refuse_final_state_mount_the_grader_cannot_read(
            self._orchestrator(tmp_path, grader)
        )


def test_the_protocol_version_names_the_by_reference_read() -> None:
    """Version 9 is the first whose GetState can answer by reference (ADR-0069)."""
    assert ENGINE_PROTOCOL_VERSION >= 9
    assert "state_by_reference" in pb2.GetStateRequest.DESCRIPTOR.fields_by_name
    assert "state_file" in pb2.GetStateResponse.DESCRIPTOR.fields_by_name
