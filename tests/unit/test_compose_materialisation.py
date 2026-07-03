"""Unit tests for ``tolokaforge.core.compose_materialisation``.

Focused on the primitives with real logic: the ``0.0.0.0 → localhost``
normalisation in :func:`resolve_host_port`, the shape parsing in
:func:`first_published_port`, the endpoint-triple composition in
:func:`resolve_env_endpoints`, and the never-raise guarantee of
:func:`shutdown_compose`.

The docker-daemon-touching call paths inside a real
``testcontainers.compose.DockerCompose`` are covered by
``tests/integration/docker/test_per_trial_runtime_backend_integration.py``.
Here we stub :class:`DockerCompose` so the tests run in-process.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.compose_materialisation import (
    RUNNER_PORT_DEFAULT,
    cleanup_partial_materialisation,
    copy_compose_context,
    first_published_port,
    make_project_temp_dir,
    resolve_env_endpoints,
    resolve_host_port,
    resolve_rag_url,
    resolve_runner_endpoint,
    shutdown_compose,
)

pytestmark = pytest.mark.unit


class TestMakeProjectTempDir:
    def test_slug_embedded_in_basename(self, tmp_path: Path) -> None:
        """Trial-id-shaped slugs land in the temp-dir basename so
        docker compose auto-generates a unique project name per
        materialisation."""
        d = make_project_temp_dir("task_id:5")
        try:
            assert "task_id_5" in d.name
            assert d.is_dir()
        finally:
            d.rmdir()

    def test_non_alphanumeric_characters_are_sanitised(self) -> None:
        d = make_project_temp_dir("task/with:mixed-chars_x")
        try:
            assert d.name.startswith("tolokaforge-")
            assert "task_with_mixed-chars_x" in d.name
        finally:
            d.rmdir()


class TestCopyComposeContext:
    def test_copies_file_and_sibling_directories(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "compose.yaml").write_text("services: {}\n")
        (src / "fixtures").mkdir()
        (src / "fixtures" / "seed.json").write_text("{}")

        dest = tmp_path / "dest"
        copy_compose_context(src / "compose.yaml", dest)

        assert (dest / "compose.yaml").read_text() == "services: {}\n"
        assert (dest / "fixtures" / "seed.json").read_text() == "{}"

    def test_creates_dest_dir_if_absent(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "compose.yaml").write_text("services: {}\n")

        dest = tmp_path / "does-not-yet-exist"
        assert not dest.exists()
        copy_compose_context(src / "compose.yaml", dest)
        assert dest.is_dir()


class TestResolveHostPort:
    """The ``0.0.0.0 → localhost`` rewrite is the load-bearing rule —
    testcontainers returns the container-listen address, not a
    reachable client host on macOS/Linux."""

    def test_returns_host_port_when_service_exposed(self) -> None:
        compose = MagicMock()
        compose.get_service_host_and_port.return_value = ("192.168.1.5", 50051)

        host, port = resolve_host_port(compose, "runner", 50051)

        assert host == "192.168.1.5"
        assert port == 50051

    def test_rewrites_0_0_0_0_to_localhost(self) -> None:
        compose = MagicMock()
        compose.get_service_host_and_port.return_value = ("0.0.0.0", 60000)

        host, port = resolve_host_port(compose, "runner", 50051)

        assert host == "localhost"
        assert port == 60000

    def test_returns_none_none_when_lookup_raises(self) -> None:
        """Testcontainers raises varied exception types (KeyError,
        ValueError, NoSuchPortExposed, subprocess errors). Every one
        should be caught and treated as "service not exposed"."""
        compose = MagicMock()
        compose.get_service_host_and_port.side_effect = KeyError("no such service")

        host, port = resolve_host_port(compose, "missing", 50051)

        assert host is None
        assert port is None


class TestFirstPublishedPort:
    def test_extracts_first_target_port(self) -> None:
        entry = MagicMock()
        entry.TargetPort = 5432
        container = MagicMock()
        container.Publishers = [entry]

        assert first_published_port(container) == 5432

    def test_skips_zero_and_non_int_targets(self) -> None:
        zero_entry = MagicMock()
        zero_entry.TargetPort = 0
        bad_entry = MagicMock()
        bad_entry.TargetPort = "not an int"
        good_entry = MagicMock()
        good_entry.TargetPort = 8000
        container = MagicMock()
        container.Publishers = [zero_entry, bad_entry, good_entry]

        assert first_published_port(container) == 8000

    def test_returns_none_when_publishers_absent(self) -> None:
        container = MagicMock(spec=[])  # No Publishers attribute.
        assert first_published_port(container) is None


class TestResolveRagUrl:
    def test_none_when_no_rag_service_declared(self) -> None:
        compose = MagicMock()
        compose.get_container.side_effect = KeyError("no rag")

        assert resolve_rag_url(compose) is None

    def test_url_when_first_candidate_resolves(self) -> None:
        container = MagicMock()
        published_entry = MagicMock()
        published_entry.TargetPort = 8080
        container.Publishers = [published_entry]

        compose = MagicMock()
        compose.get_container.return_value = container
        compose.get_service_host_and_port.return_value = ("localhost", 60080)

        assert resolve_rag_url(compose) == "http://localhost:60080"


class TestResolveRunnerEndpoint:
    def test_returns_host_port_tuple(self) -> None:
        compose = MagicMock()
        compose.get_service_host_and_port.return_value = ("localhost", 60051)

        assert resolve_runner_endpoint(compose, "runner", 50051) == ("localhost", 60051)

    def test_returns_none_when_unresolvable(self) -> None:
        compose = MagicMock()
        compose.get_service_host_and_port.side_effect = KeyError("no runner service")

        assert resolve_runner_endpoint(compose, "runner", 50051) is None

    def test_defaults_to_50051(self) -> None:
        """``RUNNER_PORT_DEFAULT`` is the convention. Callers that
        don't specify a port get it."""
        compose = MagicMock()
        compose.get_service_host_and_port.return_value = ("localhost", 60051)

        resolve_runner_endpoint(compose, "runner")
        args, kwargs = compose.get_service_host_and_port.call_args
        assert kwargs.get("port") == RUNNER_PORT_DEFAULT


class TestResolveEnvEndpoints:
    def test_composes_full_triple_with_rag(self) -> None:
        compose = MagicMock()

        def _host_port(service_name: str, port: int) -> tuple[str | None, int | None]:
            if service_name == "db":
                return ("localhost", 65432)
            if service_name == "rag":
                return ("localhost", 68080)
            return (None, None)

        compose.get_service_host_and_port.side_effect = lambda service_name, port: _host_port(
            service_name, port
        )
        rag_container = MagicMock()
        rag_container.Publishers = [MagicMock(TargetPort=8080)]
        compose.get_container.return_value = rag_container

        endpoints = resolve_env_endpoints(compose, "localhost", 60051)

        assert endpoints is not None
        assert endpoints.runner_url == "http://localhost:60051"
        assert endpoints.db_url == "http://localhost:65432"
        assert endpoints.rag_url == "http://localhost:68080"

    def test_returns_none_when_db_not_exposed(self) -> None:
        """The db service is required (endpoint-resolution
        customisation is a follow-up). Absent db → typed None,
        caller surfaces a ProvisionError."""
        compose = MagicMock()
        compose.get_service_host_and_port.side_effect = KeyError("no db")

        assert resolve_env_endpoints(compose, "localhost", 60051) is None

    def test_rag_absent_is_none_not_failure(self) -> None:
        """rag is best-effort — absent rag doesn't fail endpoint
        resolution; it just leaves ``rag_url = None`` on the
        endpoints triple."""
        compose = MagicMock()

        def _host_port(service_name: str, port: int) -> tuple[str | None, int | None]:
            if service_name == "db":
                return ("localhost", 65432)
            return (None, None)

        compose.get_service_host_and_port.side_effect = lambda service_name, port: _host_port(
            service_name, port
        )
        compose.get_container.side_effect = KeyError("no rag")

        endpoints = resolve_env_endpoints(compose, "localhost", 60051)

        assert endpoints is not None
        assert endpoints.db_url == "http://localhost:65432"
        assert endpoints.rag_url is None


class TestShutdownCompose:
    def test_calls_stop_down(self) -> None:
        compose = MagicMock()

        shutdown_compose(compose)

        compose.stop.assert_called_once_with(down=True)

    def test_swallows_exception(self) -> None:
        """`shutdown_compose` is a best-effort teardown. Errors from
        ``docker compose down`` (a common source of noise at the end of
        a run) must not propagate."""
        compose = MagicMock()
        compose.stop.side_effect = RuntimeError("docker daemon disconnected")

        shutdown_compose(compose)  # must not raise


class TestCleanupPartialMaterialisation:
    def test_none_compose_still_removes_temp_dir(self, tmp_path: Path) -> None:
        """Early-failure case: compose was never constructed. The
        temp dir still gets cleaned up."""
        d = tmp_path / "materialisation"
        d.mkdir()
        (d / "some-file").write_text("stray")

        cleanup_partial_materialisation(None, d)

        assert not d.exists()

    def test_compose_is_shutdown_and_temp_dir_removed(self, tmp_path: Path) -> None:
        compose = MagicMock()
        d = tmp_path / "materialisation"
        d.mkdir()

        cleanup_partial_materialisation(compose, d)

        compose.stop.assert_called_once_with(down=True)
        assert not d.exists()

    def test_missing_temp_dir_does_not_raise(self, tmp_path: Path) -> None:
        """Teardown is idempotent — a temp dir already removed
        (or never created) doesn't cause a failure."""
        cleanup_partial_materialisation(None, tmp_path / "does-not-exist")
