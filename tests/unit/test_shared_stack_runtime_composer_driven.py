"""Composer-driven wiring on :class:`SharedStackRuntimeBackend`.

Locks the connect / provision / rpc / close seams the plan Stage 3
introduces: the backend hands work to the injected
:class:`SubstrateComposer` and reads state back from the returned
:class:`RunSubstrate` / :class:`ComposedEnvHandle`. Deferred-connect
gating on trial-owned runner clients is asserted here — it lives on the
backend (not the composer) so the run-owned / trial-owned split stays
behind the composer's :meth:`runner_client_for` seam.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.composition_runtime import ComposedEnvHandle, RunSubstrate
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — minimal manifest + trial spec drivers
# ---------------------------------------------------------------------------


def _manifest_with_stub_compose(tmp_path: Path) -> EnvironmentManifest:
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  runner:\n"
        "    image: tolokaforge-runner:local\n"
        "    ports:\n"
        '      - "50051"\n'
    )
    return EnvironmentManifest(compose_file=compose_file, runner_service="runner")


def _trial_spec(manifest: EnvironmentManifest, *, trial_id: str = "task-1:0") -> TrialSpec:
    from tests.canonical._factories import make_task_description

    return TrialSpec(
        trial_id=trial_id,
        run_id="run-x",
        task=make_task_description(task_id="task-1", environment_manifest=manifest),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432", runner_url="http://placeholder:50051"
        ),
    )


def _make_run_sub(runner_client: Any = None) -> RunSubstrate:
    return RunSubstrate(
        run_id="run-x",
        run_stack_handles=(),
        task_stack_handles={},
        runner_client=runner_client,
        endpoints=EnvEndpoints(db_url=None, rag_url=None, runner_url="http://x:1"),
        seeds={},
        mount_docker_socket=False,
        log_capture=None,
        events=MagicMock(),
    )


def _make_env_handle(
    trial_id: str,
    *,
    trial_runner_client: Any = None,
) -> ComposedEnvHandle:
    return ComposedEnvHandle(
        trial_id=trial_id,
        trial_stack_handles=(),
        trial_endpoints=EnvEndpoints(db_url=None, rag_url=None, runner_url="http://trial:1"),
        trial_runner_client=trial_runner_client,
    )


def _make_composer(
    *,
    materialise_return: RunSubstrate,
    provision_return: ComposedEnvHandle | None = None,
    resolve_client: Callable[..., Any] | None = None,
) -> MagicMock:
    """Wire a MagicMock composer with the three seams the backend uses."""
    composer = MagicMock()
    composer.materialise_run.return_value = materialise_return
    if provision_return is not None:
        composer.provision_trial.return_value = provision_return
    if resolve_client is not None:
        composer.runner_client_for.side_effect = resolve_client
    return composer


# ---------------------------------------------------------------------------
# Connect / materialise_run
# ---------------------------------------------------------------------------


class TestConnectSeams:
    def test_connect_calls_composer_materialise_with_plan_from_manifest(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        run_client = MagicMock()
        run_sub = _make_run_sub(runner_client=run_client)
        composer = _make_composer(materialise_return=run_sub)

        backend = SharedStackRuntimeBackend(
            env_manifest=manifest,
            run_id="run-x",
            composer=composer,
        )
        backend.connect(timeout=7.5, retry_interval=0.25)

        composer.materialise_run.assert_called_once()
        plan_arg = composer.materialise_run.call_args.kwargs["plan"]
        assert plan_arg == list(manifest.stacks)
        assert backend._run_substrate is run_sub
        run_client.connect.assert_called_once_with(timeout=7.5, retry_interval=0.25)

    def test_second_connect_is_idempotent_no_op(self, tmp_path: Path) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        composer = _make_composer(materialise_return=_make_run_sub(runner_client=MagicMock()))

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.connect()

        composer.materialise_run.assert_called_once()


# ---------------------------------------------------------------------------
# Provision / provision_trial
# ---------------------------------------------------------------------------


class TestProvisionSeams:
    def test_provision_routes_through_composer_and_caches_handle(self, tmp_path: Path) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        run_sub = _make_run_sub(runner_client=MagicMock())
        env_handle = _make_env_handle("task-1:0")
        composer = _make_composer(
            materialise_return=run_sub,
            provision_return=env_handle,
        )
        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()

        spec = _trial_spec(manifest)
        result = backend.provision(spec)

        composer.provision_trial.assert_called_once()
        call = composer.provision_trial.call_args
        assert call.kwargs["plan"] == list(manifest.stacks)
        assert call.kwargs["spec"] is spec
        assert call.kwargs["run_sub"] is run_sub
        assert result is env_handle
        assert backend._env_handles[spec.trial_id] is env_handle

    def test_teardown_routes_through_composer_and_drops_cache(self, tmp_path: Path) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        run_sub = _make_run_sub(runner_client=MagicMock())
        env_handle = _make_env_handle("task-1:0")
        composer = _make_composer(
            materialise_return=run_sub,
            provision_return=env_handle,
        )
        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.provision(_trial_spec(manifest))

        backend.teardown(env_handle)

        composer.teardown_trial.assert_called_once_with(env_handle)
        assert env_handle.trial_id not in backend._env_handles
        assert env_handle.trial_id not in backend._connected_trials


# ---------------------------------------------------------------------------
# RPC methods route through runner_client_for
# ---------------------------------------------------------------------------


class TestRpcRouting:
    def test_register_trial_routes_through_composer_runner_client_for(self, tmp_path: Path) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        run_client = MagicMock()
        run_sub = _make_run_sub(runner_client=run_client)
        env_handle = _make_env_handle("task-1:0")
        composer = _make_composer(
            materialise_return=run_sub,
            provision_return=env_handle,
            resolve_client=lambda run_sub_arg, env_handle_arg: run_client,
        )
        run_client.register_trial.return_value = {"success": True}

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.provision(_trial_spec(manifest))

        result = backend.register_trial(trial_id="task-1:0", trial_spec_json="{}")

        composer.runner_client_for.assert_called_once_with(run_sub, env_handle)
        run_client.register_trial.assert_called_once()
        assert result == {"success": True}


# ---------------------------------------------------------------------------
# Deferred-connect gate for trial-owned runner clients
# ---------------------------------------------------------------------------


class TestDeferredConnectGate:
    def test_first_rpc_connects_trial_owned_client_once(self, tmp_path: Path) -> None:
        """A TRIAL_SCOPED_ONLY plan's :class:`ComposedEnvHandle` carries
        ``trial_runner_client``; the backend must connect it on the
        first per-trial RPC and skip on subsequent calls."""
        manifest = _manifest_with_stub_compose(tmp_path)
        run_sub = _make_run_sub(runner_client=None)  # no run-scope runner
        trial_client = MagicMock()
        trial_client.register_trial.return_value = {"success": True}
        trial_client.execute_tool.return_value = MagicMock()
        env_handle = _make_env_handle("task-1:0", trial_runner_client=trial_client)
        composer = _make_composer(
            materialise_return=run_sub,
            provision_return=env_handle,
            resolve_client=lambda run_sub_arg, env_handle_arg: trial_client,
        )

        backend = SharedStackRuntimeBackend(
            env_manifest=manifest,
            composer=composer,
            connect_timeout=12.0,
            connect_retry_interval=0.5,
        )
        backend.connect()
        backend.provision(_trial_spec(manifest))

        backend.register_trial(trial_id="task-1:0", trial_spec_json="{}")
        backend.register_trial(trial_id="task-1:0", trial_spec_json="{}")

        trial_client.connect.assert_called_once_with(timeout=12.0, retry_interval=0.5)
        assert "task-1:0" in backend._connected_trials

    def test_run_owned_client_skips_deferred_connect(self, tmp_path: Path) -> None:
        """A SINGLE_RUN plan has ``trial_runner_client=None`` on every
        env handle. The run-scope client was already connected at
        :meth:`connect`; the backend must not reconnect it at RPC time."""
        manifest = _manifest_with_stub_compose(tmp_path)
        run_client = MagicMock()
        run_sub = _make_run_sub(runner_client=run_client)
        env_handle = _make_env_handle("task-1:0", trial_runner_client=None)
        composer = _make_composer(
            materialise_return=run_sub,
            provision_return=env_handle,
            resolve_client=lambda run_sub_arg, env_handle_arg: run_client,
        )
        run_client.register_trial.return_value = {"success": True}

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.provision(_trial_spec(manifest))

        run_client.connect.reset_mock()  # forget the run-scope connect
        backend.register_trial(trial_id="task-1:0", trial_spec_json="{}")

        run_client.connect.assert_not_called()
        assert "task-1:0" not in backend._connected_trials


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestCloseSeam:
    def test_close_hands_substrate_to_composer_teardown_run(self, tmp_path: Path) -> None:
        manifest = _manifest_with_stub_compose(tmp_path)
        run_sub = _make_run_sub(runner_client=MagicMock())
        composer = _make_composer(materialise_return=run_sub)

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.close()

        composer.teardown_run.assert_called_once_with(run_sub)
