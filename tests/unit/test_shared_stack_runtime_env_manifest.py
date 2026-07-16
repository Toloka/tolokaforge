"""Unit tests for :class:`SharedStackRuntimeBackend`'s task-declared-stack
mode.

Covers the task-declared-stack extension: when ``env_manifest`` is set
at construction, the backend materialises the task-declared compose
stack **once at ``connect`` time** for the whole run, resolves
endpoints from it, and
tears it down at ``close``. Contrast with the pre-existing built-in-stack
mode (``runner_address`` + ``endpoints`` injected at construction), which
is unchanged and covered by the existing test suite.

Real docker-daemon interaction lives in the docker integration tests; here
we stub :class:`DockerCompose` and the compose-materialisation primitives
so the tests run in-process.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest

pytestmark = pytest.mark.unit


def _make_manifest(tmp_path: Path) -> EnvironmentManifest:
    """Author a minimal valid manifest whose compose file exists on disk.

    ``EnvironmentManifest`` validates that ``compose_file`` exists and
    parses as a YAML mapping; the fixture here writes a two-service stub
    (runner + db-service) that satisfies the manifest validator without
    needing a real docker daemon."""
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  runner:\n"
        "    image: tolokaforge-runner:local\n"
        "    ports:\n"
        '      - "50051"\n'
        "  db-service:\n"
        "    image: tolokaforge-db-service:local\n"
        "    ports:\n"
        '      - "8000"\n'
    )
    return EnvironmentManifest(compose_file=compose_file, runner_service="runner")


class TestBackwardCompatibility:
    """The built-in-stack mode (no env_manifest) must be unchanged."""

    def test_default_construction_still_works(self) -> None:
        backend = SharedStackRuntimeBackend()
        assert backend.runner_client is not None
        assert backend._endpoints is not None
        assert backend._env_manifest is None
        assert backend._compose is None

    def test_endpoints_injection_still_works(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db.example:8000",
            rag_url=None,
            runner_url="http://runner.example:50051",
        )
        backend = SharedStackRuntimeBackend(
            runner_address="runner.example:50051", endpoints=endpoints
        )
        assert backend._endpoints is endpoints


class TestEnvManifestConstruction:
    """Construction-time invariants when ``env_manifest`` is set."""

    def test_env_manifest_mode_defers_client_and_endpoints(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="my-run")

        assert backend._env_manifest is manifest
        assert backend._run_id == "my-run"
        assert backend.runner_client is None
        assert backend._endpoints is None
        assert backend._compose is None
        assert backend._temp_dir is None

    def test_env_manifest_plus_endpoints_raises(self, tmp_path: Path) -> None:
        """The two modes are mutually exclusive — passing both is a
        contract violation, fail loud instead of silently ignoring one."""
        manifest = _make_manifest(tmp_path)
        endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
        with pytest.raises(ValueError, match="env_manifest OR endpoints"):
            SharedStackRuntimeBackend(env_manifest=manifest, endpoints=endpoints)


class TestConnectMaterialises:
    """``connect`` materialises the task-declared stack, resolves
    endpoints, then wires the gRPC runner client."""

    def test_connect_materialises_and_populates_state(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

        fake_compose = MagicMock()
        fake_endpoints = EnvEndpoints(
            db_url="http://localhost:65432",
            rag_url=None,
            runner_url="http://localhost:60051",
        )
        with (
            patch("tolokaforge.core.shared_stack_runtime.DockerCompose", return_value=fake_compose),
            patch(
                "tolokaforge.core.shared_stack_runtime.copy_compose_context",
                lambda src, dst: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.apply_network_policy_to_compose_file",
                lambda *a, **k: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.resolve_runner_endpoint",
                return_value=("localhost", 60051),
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.resolve_env_endpoints",
                return_value=fake_endpoints,
            ),
            patch("tolokaforge.core.shared_stack_runtime.GrpcRunnerClient") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            backend.connect()

        # Materialised state is preserved on the backend for teardown.
        assert backend._compose is fake_compose
        assert backend._temp_dir is not None
        assert backend._endpoints is fake_endpoints
        assert backend.runner_client is mock_client
        # runner client was constructed with the resolved host:port.
        mock_client_cls.assert_called_once_with(runner_address="localhost:60051")
        mock_client.connect.assert_called_once()

    def test_connect_raises_provision_error_when_runner_missing(self, tmp_path: Path) -> None:
        """A compose file whose runner_service isn't exposed fails loud
        (ProvisionError with a message pointing at the runner_service)."""
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

        fake_compose = MagicMock()
        with (
            patch("tolokaforge.core.shared_stack_runtime.DockerCompose", return_value=fake_compose),
            patch(
                "tolokaforge.core.shared_stack_runtime.copy_compose_context",
                lambda src, dst: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.apply_network_policy_to_compose_file",
                lambda *a, **k: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.resolve_runner_endpoint",
                return_value=None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.cleanup_partial_materialisation"
            ) as mock_cleanup,
            pytest.raises(ProvisionError, match="runner_service"),
        ):
            backend.connect()
        # Partial materialisation cleaned up before we raised.
        mock_cleanup.assert_called_once()

    def test_connect_accepts_db_url_none(self, tmp_path: Path) -> None:
        """``db_url`` is best-effort in env_manifest mode. When the task's
        compose file omits ``db-service:8000`` the resolver returns
        ``EnvEndpoints(db_url=None, ...)`` and connect proceeds — the
        runner-side ``DBServiceClient`` binds to ``DB_SERVICE_URL`` from
        its container env, so a missing db_url is not a provisioning
        failure. Load-bearing contract change (was: ProvisionError)."""
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

        fake_compose = MagicMock()
        fake_endpoints = EnvEndpoints(
            db_url=None,
            rag_url=None,
            runner_url="http://localhost:60051",
        )
        with (
            patch("tolokaforge.core.shared_stack_runtime.DockerCompose", return_value=fake_compose),
            patch(
                "tolokaforge.core.shared_stack_runtime.copy_compose_context",
                lambda src, dst: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.apply_network_policy_to_compose_file",
                lambda *a, **k: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.resolve_runner_endpoint",
                return_value=("localhost", 60051),
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.resolve_env_endpoints",
                return_value=fake_endpoints,
            ),
            patch("tolokaforge.core.shared_stack_runtime.GrpcRunnerClient") as mock_client_cls,
        ):
            mock_client_cls.return_value = MagicMock()
            backend.connect()

        assert backend._endpoints is fake_endpoints
        assert backend._endpoints.db_url is None

    def test_connect_raises_provision_error_on_compose_start_failure(self, tmp_path: Path) -> None:
        """A docker daemon failure during compose up surfaces as a typed
        ProvisionError with the underlying cause attached."""
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

        fake_compose = MagicMock()
        fake_compose.start.side_effect = RuntimeError("daemon socket refused")
        with (
            patch("tolokaforge.core.shared_stack_runtime.DockerCompose", return_value=fake_compose),
            patch(
                "tolokaforge.core.shared_stack_runtime.copy_compose_context",
                lambda src, dst: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.apply_network_policy_to_compose_file",
                lambda *a, **k: None,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.cleanup_partial_materialisation"
            ) as mock_cleanup,
            pytest.raises(ProvisionError, match="docker compose up failed"),
        ):
            backend.connect()
        mock_cleanup.assert_called_once()


class TestCloseIdempotency:
    """``close`` is safe to call in any state (before materialisation,
    after materialisation, twice) — the shared-run lifecycle."""

    def test_close_without_materialisation_is_noop(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
        # Never called connect — runner_client and _compose are None.
        backend.close()  # must not raise

    def test_close_shuts_down_compose_and_removes_temp_dir(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
        # Simulate a successful materialisation state.
        fake_compose = MagicMock()
        fake_client = MagicMock()
        real_temp = tmp_path / "materialised"
        real_temp.mkdir()
        backend._compose = fake_compose
        backend._temp_dir = real_temp
        backend.runner_client = fake_client

        backend.close()

        fake_client.close.assert_called_once()
        fake_compose.stop.assert_called_once_with(down=True)
        assert not real_temp.exists()
        # State cleared so a second close is a no-op.
        assert backend._compose is None
        assert backend._temp_dir is None

    def test_close_twice_is_idempotent(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
        fake_compose = MagicMock()
        real_temp = tmp_path / "materialised"
        real_temp.mkdir()
        backend._compose = fake_compose
        backend._temp_dir = real_temp
        backend.runner_client = MagicMock()

        backend.close()
        backend.close()  # must not raise

    def test_close_tears_down_compose_even_if_client_close_raises(self, tmp_path: Path) -> None:
        """If the runner client's close raises (e.g. broken gRPC channel),
        the compose stack + temp dir must still be cleaned up — otherwise
        a leaked docker project outlives the run."""
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
        fake_compose = MagicMock()
        real_temp = tmp_path / "materialised"
        real_temp.mkdir()
        fake_client = MagicMock()
        fake_client.close.side_effect = RuntimeError("gRPC channel broken")
        backend._compose = fake_compose
        backend._temp_dir = real_temp
        backend.runner_client = fake_client

        with pytest.raises(RuntimeError, match="gRPC channel broken"):
            backend.close()

        # Downstream teardown still ran.
        fake_compose.stop.assert_called_once_with(down=True)
        assert not real_temp.exists()


class TestMaterialiseIdempotent:
    """A second ``connect()`` in env_manifest mode must not clobber the
    running stack. Mirrors ``GrpcRunnerClient.connect``'s
    ``if self.channel is None`` guard."""

    def test_double_connect_does_not_re_materialise(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
        # Simulate a successful first materialisation.
        fake_compose = MagicMock()
        backend._compose = fake_compose
        backend._temp_dir = tmp_path / "already-there"
        backend._temp_dir.mkdir()
        backend._endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
        backend.runner_client = MagicMock()

        # Second _materialise_manifest is the early-return guard.
        with patch("tolokaforge.core.shared_stack_runtime.DockerCompose") as mock_docker_compose:
            backend._materialise_manifest()
            mock_docker_compose.assert_not_called()

        # State preserved — no clobber.
        assert backend._compose is fake_compose
