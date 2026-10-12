"""Contract of the standalone trial entry ``run_trial_from_inputs``.

Hermetic: the single-task adapter and the registry loaders are patched at the
``trial_node`` binding, so no fixture pack, Docker, or LLM key is needed. The
canonical tier (``tests/canonical/test_run_trial_node.py``) runs real adapters
over fixture packs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import tolokaforge.core.trial_node as trial_node_mod
from tests.canonical._factories import (
    make_env_endpoints,
    make_task_config,
    make_task_description,
)
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.models import GraderConfig, ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.plugin_registry import TrialGraderContext
from tolokaforge.core.runtime import InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial import TrialNodeInputs, TrialSpec
from tolokaforge.core.trial_node import (
    TaskPackDriftError,
    compute_task_pack_hash,
    run_trial_from_inputs,
)
from tolokaforge.secrets import CONTAINER_SECRETS_ENV_VAR, get_default

pytestmark = pytest.mark.unit

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


class _FakeAdapter:
    trial_grader_name = "runner_rpc"

    def register_preloaded_task(self, task: Any, task_dir: Path) -> None:
        self.preloaded = (task, task_dir)


@pytest.fixture(autouse=True)
def _fake_adapter(monkeypatch: pytest.MonkeyPatch) -> _FakeAdapter:
    adapter = _FakeAdapter()
    monkeypatch.setattr(trial_node_mod, "get_adapter", lambda adapter_type, params: adapter)
    return adapter


def _spec(task_id: str = "task-1") -> TrialSpec:
    return TrialSpec(
        trial_id=f"{task_id}:0",
        run_id="run-1",
        task=make_task_description(task_id=task_id),
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )


def _inputs(**overrides: Any) -> TrialNodeInputs:
    fields: dict[str, Any] = {
        "task": make_task_config(task_id="task-1"),
        "trial_spec": _spec(),
        "grader_config": GraderConfig(name="runner_rpc"),
        "grpc_max_message_mb": 7,
        "runtime": "in_memory",
        "conductor": "in_memory",
    }
    fields.update(overrides)
    return TrialNodeInputs(**fields)


class TestInputsContract:
    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        inputs = _inputs(task_pack_dir=tmp_path, task_pack_hash=compute_task_pack_hash(tmp_path))
        again = TrialNodeInputs.model_validate_json(inputs.model_dump_json())
        assert again == inputs

    @pytest.mark.parametrize("field", ["runner_address", "grader_address"])
    def test_remote_addresses_are_not_part_of_the_contract(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _inputs(**{field: "host:1234"})

    def test_pack_dir_without_hash_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="set together"):
            _inputs(task_pack_dir=tmp_path)

    def test_hash_without_pack_dir_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="set together"):
            _inputs(task_pack_hash="sha256:" + "0" * 64)

    def test_spec_describing_another_task_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="describe different tasks"):
            _inputs(trial_spec=_spec(task_id="other"))


class TestTaskPackHashGuard:
    def _pack(self, tmp_path: Path) -> Path:
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "grading.yaml").write_text("combine: {}\n")
        return pack

    def test_matching_pack_runs(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path)
        result = run_trial_from_inputs(
            _inputs(task_pack_dir=pack, task_pack_hash=compute_task_pack_hash(pack))
        )
        assert result.trial_id == "task-1:0"

    def test_drifted_pack_fails_loud_before_any_backend_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pack = self._pack(tmp_path)
        inputs = _inputs(task_pack_dir=pack, task_pack_hash=compute_task_pack_hash(pack))
        (pack / "grading.yaml").write_text("combine: {method: changed}\n")

        def no_backend(name: str) -> Any:
            raise AssertionError("a drifted pack must not reach backend construction")

        monkeypatch.setattr(trial_node_mod, "load_runtime_backend", no_backend)
        with pytest.raises(TaskPackDriftError, match="drifted"):
            run_trial_from_inputs(inputs)

    def test_added_file_changes_the_hash(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path)
        before = compute_task_pack_hash(pack)
        (pack / "extra.json").write_text("{}")
        assert compute_task_pack_hash(pack) != before


class TestProvisionErrorPropagates:
    def test_provision_failure_raises_and_backend_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = InMemoryRuntimeBackend(await_ready_times_out=True)
        monkeypatch.setattr(
            trial_node_mod, "load_runtime_backend", lambda name: lambda ctx: backend
        )
        with pytest.raises(ProvisionError):
            run_trial_from_inputs(_inputs())
        assert backend.call_log.close_calls == 1


class TestGraderRouting:
    def test_per_trial_runtime_grades_through_the_backend_not_a_dialled_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-trial backend owns one runner endpoint per trial, so the grader
        must route through the backend; dialling ``EXECUTOR_ADDRESS`` reaches
        the wrong runner."""
        monkeypatch.setenv("EXECUTOR_ADDRESS", "wrong-host:50051")
        backends: list[Any] = []
        contexts: list[TrialGraderContext] = []

        real_loader = trial_node_mod.load_runtime_backend

        def capture_backend(name: str) -> Any:
            factory = real_loader(name)

            def build(ctx: Any) -> Any:
                backend = factory(ctx)
                backends.append(backend)
                return backend

            return build

        class _Stop(Exception):
            pass

        def capture_grader(name: str) -> Any:
            def build(ctx: TrialGraderContext) -> Any:
                contexts.append(ctx)
                raise _Stop

            return build

        monkeypatch.setattr(trial_node_mod, "load_runtime_backend", capture_backend)
        monkeypatch.setattr(trial_node_mod, "load_trial_grader", capture_grader)
        with pytest.raises(_Stop):
            run_trial_from_inputs(_inputs(runtime="per_trial"))

        (backend,) = backends
        (ctx,) = contexts
        assert isinstance(backend, PerTrialRuntimeBackend)
        assert ctx.runtime_backend is backend
        assert ctx.runner_address is None
        assert ctx.grpc_max_message_mb == 7


class TestSecretPayload:
    def test_container_payload_installs_the_process_secret_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager", get_default(), raising=True
        )
        monkeypatch.setenv(CONTAINER_SECRETS_ENV_VAR, json.dumps({"NODE_ONLY_NAME": "v"}))
        run_trial_from_inputs(_inputs())
        assert get_default().get_secret("NODE_ONLY_NAME") == "v"

    def test_malformed_payload_fails_without_echoing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONTAINER_SECRETS_ENV_VAR, '{"K": "sekrit-value"')
        with pytest.raises(ValueError, match="not valid JSON") as exc:
            run_trial_from_inputs(_inputs())
        assert "sekrit-value" not in str(exc.value)

    def test_non_object_payload_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CONTAINER_SECRETS_ENV_VAR, "[1]")
        with pytest.raises(ValueError, match="JSON object"):
            run_trial_from_inputs(_inputs())


def test_base_adapter_is_not_node_runnable_by_default() -> None:
    stub = type(
        "StubAdapter",
        (BaseAdapter,),
        {name: (lambda self, *a, **k: None) for name in BaseAdapter.__abstractmethods__},
    )({})
    with pytest.raises(NotImplementedError, match="not node-runnable"):
        stub.register_preloaded_task(make_task_config(task_id="task-1"), Path())


class TestBackendBuildContext:
    """The backend is built with the substrate the inputs ask for."""

    def _built_context(self, monkeypatch: pytest.MonkeyPatch, inputs: TrialNodeInputs) -> Any:
        contexts: list[Any] = []

        def loader(name: str) -> Any:
            def build(ctx: Any) -> InMemoryRuntimeBackend:
                contexts.append(ctx)
                return InMemoryRuntimeBackend()

            return build

        monkeypatch.setattr(trial_node_mod, "load_runtime_backend", loader)
        run_trial_from_inputs(inputs)
        (ctx,) = contexts
        return ctx

    def test_grader_block_and_message_bound_reach_the_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._built_context(
            monkeypatch,
            _inputs(grader_config=GraderConfig(name="runner_rpc", expose_substrate=True)),
        )
        assert ctx.expose_substrate is True
        assert ctx.grpc_max_message_mb == 7
        assert ctx.mount_docker_socket is False

    def test_substrate_stays_closed_without_a_grader_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._built_context(monkeypatch, _inputs(grader_config=None))
        assert ctx.expose_substrate is False

    def test_coding_harness_model_mounts_the_docker_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness_agent = ModelConfig(provider="openai", name="gpt-4", harness="claude-code")
        spec = _spec().model_copy(update={"agent_model_config": harness_agent})
        ctx = self._built_context(monkeypatch, _inputs(trial_spec=spec))
        assert ctx.mount_docker_socket is True
