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
from testcontainers.compose.compose import ComposeContainer, PublishedPortModel

from tolokaforge.core.compose_materialisation import (
    NETPOLICY_EDGE_NETWORK,
    NETPOLICY_INTERNAL_NETWORK,
    RUNNER_PORT_DEFAULT,
    NetworkPolicyError,
    apply_network_policy_to_compose_file,
    cleanup_partial_materialisation,
    compose_container_to_snapshot,
    copy_compose_context,
    enforce_network_policy,
    first_published_port,
    make_project_temp_dir,
    resolve_env_endpoints,
    resolve_host_port,
    resolve_rag_url,
    resolve_runner_endpoint,
    shutdown_compose,
    verify_network_policy_supported,
)
from tolokaforge.core.trial import NetworkPolicy

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

    def test_rag_service_override_scans_only_named_service(self) -> None:
        """A set ``rag_service`` narrows the scan to that single service
        instead of the ``RAG_SERVICE_CANDIDATES`` convention."""
        container = MagicMock()
        container.Publishers = [MagicMock(TargetPort=8080)]
        compose = MagicMock()
        compose.get_container.return_value = container
        compose.get_service_host_and_port.return_value = ("localhost", 59090)

        assert resolve_rag_url(compose, rag_service="myrag") == "http://localhost:59090"
        compose.get_container.assert_called_once_with(service_name="myrag")

    def test_rag_port_override_bypasses_auto_detect(self) -> None:
        """A set ``rag_port`` is the container port used directly — the
        first-published-port auto-detect is skipped, so a differing
        published port is ignored."""
        container = MagicMock()
        container.Publishers = [MagicMock(TargetPort=9999)]
        compose = MagicMock()
        compose.get_container.return_value = container
        compose.get_service_host_and_port.return_value = ("localhost", 58080)

        assert (
            resolve_rag_url(compose, rag_service="rag", rag_port=8080) == "http://localhost:58080"
        )
        _, kwargs = compose.get_service_host_and_port.call_args
        assert kwargs["port"] == 8080


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
            if service_name == "db-service":
                return ("localhost", 68000)
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

        assert endpoints.runner_url == "http://localhost:60051"
        assert endpoints.db_url == "http://localhost:68000"
        assert endpoints.rag_url == "http://localhost:68080"

    def test_db_absent_yields_none_not_failure(self) -> None:
        """db_url is best-effort — a task compose file that omits
        ``db-service:8000`` gets ``EnvEndpoints(db_url=None, ...)``.
        The runner-side ``DBServiceClient`` binds to ``DB_SERVICE_URL``
        from its container env, and ``db_json.py`` tools fall back to
        the same env var when constructed without a URL."""
        compose = MagicMock()
        compose.get_service_host_and_port.side_effect = KeyError("no db-service")
        compose.get_container.side_effect = KeyError("no rag either")

        endpoints = resolve_env_endpoints(compose, "localhost", 60051)

        assert endpoints.runner_url == "http://localhost:60051"
        assert endpoints.db_url is None
        assert endpoints.rag_url is None

    def test_rag_absent_is_none_not_failure(self) -> None:
        """rag is best-effort — absent rag doesn't fail endpoint
        resolution; it just leaves ``rag_url = None`` on the
        endpoints triple."""
        compose = MagicMock()

        def _host_port(service_name: str, port: int) -> tuple[str | None, int | None]:
            if service_name == "db-service":
                return ("localhost", 68000)
            return (None, None)

        compose.get_service_host_and_port.side_effect = lambda service_name, port: _host_port(
            service_name, port
        )
        compose.get_container.side_effect = KeyError("no rag")

        endpoints = resolve_env_endpoints(compose, "localhost", 60051)

        assert endpoints.db_url == "http://localhost:68000"
        assert endpoints.rag_url is None

    def test_db_service_and_port_overrides_resolve_named_endpoint(self) -> None:
        """Non-``None`` ``db_service`` / ``db_port`` resolve that exact
        service+port instead of the ``db-service:8000`` convention."""
        compose = MagicMock()

        def _host_port(service_name: str, port: int) -> tuple[str | None, int | None]:
            if service_name == "mydb" and port == 5433:
                return ("localhost", 65433)
            return (None, None)

        compose.get_service_host_and_port.side_effect = lambda service_name, port: _host_port(
            service_name, port
        )
        compose.get_container.side_effect = KeyError("no rag")

        endpoints = resolve_env_endpoints(
            compose, "localhost", 60051, db_service="mydb", db_port=5433
        )

        assert endpoints.db_url == "http://localhost:65433"

    def test_rag_overrides_are_forwarded_to_resolve_rag_url(self) -> None:
        """``rag_service`` / ``rag_port`` reach ``resolve_rag_url`` — the
        named service is scanned at the pinned port."""
        compose = MagicMock()
        compose.get_service_host_and_port.side_effect = lambda service_name, port: (
            ("localhost", 58080) if (service_name == "myrag" and port == 8080) else (None, None)
        )
        container = MagicMock()
        container.Publishers = [MagicMock(TargetPort=9999)]
        compose.get_container.return_value = container

        endpoints = resolve_env_endpoints(
            compose, "localhost", 60051, rag_service="myrag", rag_port=8080
        )

        assert endpoints.rag_url == "http://localhost:58080"
        compose.get_container.assert_called_once_with(service_name="myrag")


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


class TestComposeContainerToSnapshot:
    """Map ``ComposeContainer`` → ``ContainerSnapshot`` — the shape the
    infrastructure sub-panel consumes via :meth:`RuntimeBackend.get_infrastructure_snapshot`.
    """

    def test_maps_full_container_shape(self) -> None:
        container = ComposeContainer(
            Name="proj-runner-1",
            Service="runner",
            State="running",
            Health="healthy",
            Publishers=[
                PublishedPortModel(TargetPort=50051, PublishedPort=60051),
                PublishedPortModel(TargetPort=8000, PublishedPort=61000),
            ],
        )

        snapshot = compose_container_to_snapshot(container)

        assert snapshot == {
            "name": "proj-runner-1",
            "service": "runner",
            "state": "running",
            "health": "healthy",
            "ports": {50051: 60051, 8000: 61000},
        }

    def test_missing_health_maps_to_none(self) -> None:
        """A compose service that declares no health probe leaves
        ``Health`` empty on the container; the snapshot renders that as
        ``None`` (falsy short-circuit) so the panel does not print a
        stale 'unhealthy' badge."""
        container = ComposeContainer(
            Name="db-1",
            Service="db",
            State="running",
            Health=None,
            Publishers=[],
        )

        snapshot = compose_container_to_snapshot(container)

        assert snapshot["health"] is None
        assert snapshot["ports"] == {}

    def test_missing_state_defaults_to_unknown(self) -> None:
        """``State`` unset — testcontainers' shape allows every field to
        be ``None``. Snapshot fills in a stable literal so downstream
        rendering does not have to branch on ``None``."""
        container = ComposeContainer(Name="c", Service="s")

        snapshot = compose_container_to_snapshot(container)

        assert snapshot["state"] == "unknown"

    def test_skips_publishers_missing_target_or_published_port(self) -> None:
        """Partial publisher rows (either half absent) are dropped so
        the ports map only carries well-formed pairs."""
        container = ComposeContainer(
            Name="c",
            Service="s",
            State="running",
            Publishers=[
                PublishedPortModel(TargetPort=50051, PublishedPort=60051),
                PublishedPortModel(TargetPort=None, PublishedPort=61000),
                PublishedPortModel(TargetPort=8000, PublishedPort=None),
            ],
        )

        snapshot = compose_container_to_snapshot(container)

        assert snapshot["ports"] == {50051: 60051}


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


def _minimal_compose() -> dict:
    """Two services, no task-declared networks (join the compose default)."""
    return {
        "services": {
            "runner": {"image": "tolokaforge-runner:local", "ports": ["50051"]},
            "app-service": {"image": "nginx:1.27-alpine", "ports": ["80"]},
        }
    }


class TestVerifyNetworkPolicySupported:
    def test_limited_internet_raises(self) -> None:
        with pytest.raises(NetworkPolicyError, match="limited_internet"):
            verify_network_policy_supported(NetworkPolicy.LIMITED_INTERNET)

    def test_error_names_323_and_alternatives(self) -> None:
        with pytest.raises(NetworkPolicyError) as excinfo:
            verify_network_policy_supported(NetworkPolicy.LIMITED_INTERNET)
        message = str(excinfo.value)
        assert "#323" in message
        assert "no_internet" in message
        assert "full_internet" in message

    @pytest.mark.parametrize("policy", [NetworkPolicy.NO_INTERNET, NetworkPolicy.FULL_INTERNET])
    def test_enforceable_policies_return_cleanly(self, policy: NetworkPolicy) -> None:
        assert verify_network_policy_supported(policy) is None


class TestEnforceNetworkPolicy:
    def test_full_internet_is_identity(self) -> None:
        doc = _minimal_compose()
        result = enforce_network_policy(doc, NetworkPolicy.FULL_INTERNET, "runner")
        assert result is doc

    def test_limited_internet_raises_belt_and_suspenders(self) -> None:
        with pytest.raises(NetworkPolicyError):
            enforce_network_policy(_minimal_compose(), NetworkPolicy.LIMITED_INTERNET, "runner")

    def test_no_internet_does_not_mutate_input(self) -> None:
        doc = _minimal_compose()
        enforce_network_policy(doc, NetworkPolicy.NO_INTERNET, "runner")
        assert "networks" not in doc
        assert "networks" not in doc["services"]["runner"]

    def test_no_internet_injects_internal_and_edge_networks(self) -> None:
        result = enforce_network_policy(_minimal_compose(), NetworkPolicy.NO_INTERNET, "runner")
        assert result["networks"][NETPOLICY_INTERNAL_NETWORK] == {"internal": True}
        assert result["networks"][NETPOLICY_EDGE_NETWORK] == {}

    def test_no_internet_attaches_every_service_to_internal(self) -> None:
        result = enforce_network_policy(_minimal_compose(), NetworkPolicy.NO_INTERNET, "runner")
        for service in result["services"].values():
            assert NETPOLICY_INTERNAL_NETWORK in service["networks"]

    def test_no_internet_runner_additionally_on_edge(self) -> None:
        result = enforce_network_policy(_minimal_compose(), NetworkPolicy.NO_INTERNET, "runner")
        assert NETPOLICY_EDGE_NETWORK in result["services"]["runner"]["networks"]
        assert NETPOLICY_EDGE_NETWORK not in result["services"]["app-service"]["networks"]

    def test_no_internet_forces_task_declared_networks_internal(self) -> None:
        """A doc that declares its own networks: those are forced
        internal:true, and the runner still gains the edge network."""
        doc = {
            "services": {
                "runner": {"image": "r:local", "networks": ["backplane"]},
                "app-service": {"image": "nginx:1.27-alpine", "networks": ["backplane"]},
            },
            "networks": {"backplane": {"driver": "bridge"}},
        }
        result = enforce_network_policy(doc, NetworkPolicy.NO_INTERNET, "runner")

        assert result["networks"]["backplane"]["internal"] is True
        assert result["networks"]["backplane"]["driver"] == "bridge"
        for service in result["services"].values():
            assert "backplane" in service["networks"]
            assert NETPOLICY_INTERNAL_NETWORK in service["networks"]
        assert NETPOLICY_EDGE_NETWORK in result["services"]["runner"]["networks"]

    def test_no_internet_preserves_mapping_form_networks(self) -> None:
        """A service using the mapping form (aliases / static IPs) keeps
        that shape and its per-network config when the internal net is
        merged in."""
        doc = {
            "services": {
                "runner": {
                    "image": "r:local",
                    "networks": {"backplane": {"aliases": ["r"]}},
                },
            },
            "networks": {"backplane": {}},
        }
        result = enforce_network_policy(doc, NetworkPolicy.NO_INTERNET, "runner")
        nets = result["services"]["runner"]["networks"]
        assert isinstance(nets, dict)
        assert nets["backplane"] == {"aliases": ["r"]}
        assert NETPOLICY_INTERNAL_NETWORK in nets
        assert NETPOLICY_EDGE_NETWORK in nets


class TestApplyNetworkPolicyToComposeFile:
    def test_full_internet_leaves_file_byte_identical(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        original = "services:\n  runner:\n    image: r:local  # keep this comment\n"
        compose.write_text(original)
        apply_network_policy_to_compose_file(compose, NetworkPolicy.FULL_INTERNET, "runner")
        assert compose.read_text() == original

    def test_no_internet_rewrites_file_with_injected_networks(self, tmp_path: Path) -> None:
        import yaml

        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  runner:\n    image: r:local\n")
        apply_network_policy_to_compose_file(compose, NetworkPolicy.NO_INTERNET, "runner")

        doc = yaml.safe_load(compose.read_text())
        assert doc["networks"][NETPOLICY_INTERNAL_NETWORK] == {"internal": True}
        assert NETPOLICY_EDGE_NETWORK in doc["services"]["runner"]["networks"]
