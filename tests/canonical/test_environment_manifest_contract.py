"""Pin the JSON wire shape and validators of ``EnvironmentManifest``.

Contract tests for ADR-0009. The manifest is the typed declaration of a
task's multi-service world; this module pins every field, every default,
every cross-field validator. Any silent change to the wire format or to a
validator surfaces here.

The manifest carries no runtime behaviour in v0.5.0 — no provisioner reads
it yet. The contract test IS the only consumer; that is intentional per
ADR-0009.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.runner.models import (
    EnvironmentManifest,
    HealthProbe,
    InitialStateRef,
    PortSpec,
    Resources,
    ServiceSpec,
    TaskDescription,
    VolumeMount,
)

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_service(name: str = "runner") -> ServiceSpec:
    return ServiceSpec(name=name, image="tolokaforge/runner:0.5.0")


def _make_minimal_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(services=[_make_minimal_service()])


# ---------------------------------------------------------------------------
# HealthProbe
# ---------------------------------------------------------------------------


class TestHealthProbeContract:
    def test_tcp_probe_minimal(self) -> None:
        probe = HealthProbe(kind="tcp", port=5432)
        assert probe.path is None
        assert probe.interval_seconds == 5
        assert probe.timeout_seconds == 3
        assert probe.retries == 10

    def test_http_probe_requires_path(self) -> None:
        with pytest.raises(ValidationError, match="path is required"):
            HealthProbe(kind="http", port=8080)

    def test_tcp_probe_rejects_path(self) -> None:
        with pytest.raises(ValidationError, match="not allowed"):
            HealthProbe(kind="tcp", port=5432, path="/healthz")

    def test_http_probe_round_trip(self) -> None:
        probe = HealthProbe(
            kind="http",
            port=8080,
            path="/healthz",
            interval_seconds=10,
            timeout_seconds=2,
            retries=20,
        )
        assert HealthProbe.model_validate_json(probe.model_dump_json()) == probe

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HealthProbe.model_validate({"kind": "tcp", "port": 5432, "extra": "no"})


# ---------------------------------------------------------------------------
# PortSpec
# ---------------------------------------------------------------------------


class TestPortSpecContract:
    def test_default_protocol_is_tcp(self) -> None:
        spec = PortSpec(container_port=80)
        assert spec.protocol == "tcp"

    def test_udp_accepted(self) -> None:
        spec = PortSpec(container_port=53, protocol="udp")
        assert spec.protocol == "udp"

    def test_invalid_protocol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortSpec.model_validate({"container_port": 80, "protocol": "sctp"})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortSpec.model_validate({"container_port": 80, "host_port": 8080})


# ---------------------------------------------------------------------------
# VolumeMount
# ---------------------------------------------------------------------------


class TestVolumeMountContract:
    def test_minimal_mount(self) -> None:
        mount = VolumeMount(source="data", target="/var/lib/data")
        assert mount.read_only is False

    def test_read_only_round_trip(self) -> None:
        mount = VolumeMount(source="fixtures/seed.sql", target="/seed.sql", read_only=True)
        assert VolumeMount.model_validate_json(mount.model_dump_json()) == mount

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VolumeMount.model_validate({"source": "a", "target": "/b", "mode": "rw"})


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class TestResourcesContract:
    def test_empty_resources_allowed(self) -> None:
        r = Resources()
        assert r.cpu is None
        assert r.memory is None

    def test_k8s_quantity_strings_accepted(self) -> None:
        r = Resources(cpu="500m", memory="512Mi")
        assert r.cpu == "500m"
        assert r.memory == "512Mi"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Resources.model_validate({"cpu": "2", "gpu": "1"})


# ---------------------------------------------------------------------------
# InitialStateRef
# ---------------------------------------------------------------------------


class TestInitialStateRefContract:
    def test_round_trip_via_alias(self) -> None:
        ref = InitialStateRef.model_validate({"from": "fixtures/orders.sql"})
        assert ref.from_ == "fixtures/orders.sql"
        dumped = ref.model_dump(by_alias=True)
        assert dumped == {"from": "fixtures/orders.sql"}

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": "a.sql", "format": "sql"})


# ---------------------------------------------------------------------------
# ServiceSpec
# ---------------------------------------------------------------------------


class TestServiceSpecContract:
    def test_minimal_construction(self) -> None:
        svc = _make_minimal_service()
        assert svc.command is None
        assert svc.env == {}
        assert svc.ports == []
        assert svc.volumes == []
        assert svc.depends_on == []
        assert svc.health is None

    def test_full_construction_round_trip(self) -> None:
        svc = ServiceSpec(
            name="db",
            image="postgres:16",
            command=["postgres", "-c", "shared_buffers=256MB"],
            env={"POSTGRES_PASSWORD": "dev"},
            ports=[PortSpec(container_port=5432)],
            volumes=[VolumeMount(source="pgdata", target="/var/lib/postgresql/data")],
            depends_on=[],
            health=HealthProbe(kind="tcp", port=5432),
        )
        assert ServiceSpec.model_validate_json(svc.model_dump_json()) == svc

    def test_latest_tag_rejected(self) -> None:
        with pytest.raises(ValidationError, match="pinned"):
            ServiceSpec(name="runner", image="tolokaforge/runner:latest")

    def test_bare_image_rejected(self) -> None:
        with pytest.raises(ValidationError, match="explicit tag or"):
            ServiceSpec(name="runner", image="tolokaforge/runner")

    def test_sha_digest_accepted(self) -> None:
        svc = ServiceSpec(
            name="runner",
            image="tolokaforge/runner@sha256:" + "0" * 64,
        )
        assert svc.image.startswith("tolokaforge/runner@sha256:")

    @pytest.mark.parametrize(
        "bad_name",
        [
            "Runner",  # uppercase
            "-leading-dash",
            "trailing-dash-",
            "with_underscore",
            "with.dot",
            "",
        ],
    )
    def test_invalid_dns_label_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError, match="DNS label"):
            ServiceSpec(name=bad_name, image="tolokaforge/runner:0.5.0")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServiceSpec.model_validate(
                {"name": "runner", "image": "tolokaforge/runner:0.5.0", "restart": "always"}
            )


# ---------------------------------------------------------------------------
# EnvironmentManifest — top-level model + cross-service validators
# ---------------------------------------------------------------------------


class TestEnvironmentManifestContract:
    def test_minimal_manifest(self) -> None:
        manifest = _make_minimal_manifest()
        assert manifest.initial_state == {}
        assert manifest.resources is None

    def test_empty_services_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            EnvironmentManifest(services=[])

    def test_duplicate_service_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate name"):
            EnvironmentManifest(
                services=[
                    _make_minimal_service("runner"),
                    _make_minimal_service("runner"),
                ]
            )

    def test_depends_on_must_resolve(self) -> None:
        with pytest.raises(ValidationError, match="unknown service"):
            EnvironmentManifest(
                services=[
                    ServiceSpec(
                        name="runner",
                        image="tolokaforge/runner:0.5.0",
                        depends_on=["nonexistent"],
                    ),
                ]
            )

    def test_initial_state_keys_must_resolve(self) -> None:
        with pytest.raises(ValidationError, match="does not match any declared service"):
            EnvironmentManifest(
                services=[_make_minimal_service("runner")],
                initial_state={"db": InitialStateRef.model_validate({"from": "fixture.sql"})},
            )

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EnvironmentManifest.model_validate(
                {
                    "services": [{"name": "runner", "image": "tolokaforge/runner:0.5.0"}],
                    "version": "1.0",
                }
            )

    def test_canonical_wire_shape(self) -> None:
        """Pin the JSON wire shape of a fully-populated manifest. Any
        intentional field addition or default change must update this
        snapshot in the same PR."""
        manifest = EnvironmentManifest(
            services=[
                ServiceSpec(name="runner", image="tolokaforge/runner:0.5.0"),
                ServiceSpec(
                    name="db",
                    image="postgres:16",
                    env={"POSTGRES_PASSWORD": "dev"},
                    ports=[PortSpec(container_port=5432)],
                    health=HealthProbe(kind="tcp", port=5432),
                ),
                ServiceSpec(
                    name="backend",
                    image="ghcr.io/example/api:v1.2.3",
                    depends_on=["db"],
                    health=HealthProbe(kind="http", port=8080, path="/healthz"),
                ),
            ],
            initial_state={"db": InitialStateRef.model_validate({"from": "fixtures/seed.sql"})},
            resources=Resources(cpu="2", memory="4Gi"),
        )
        actual = json.loads(manifest.model_dump_json(by_alias=True))
        expected: dict[str, Any] = {
            "services": [
                {
                    "name": "runner",
                    "image": "tolokaforge/runner:0.5.0",
                    "command": None,
                    "env": {},
                    "ports": [],
                    "volumes": [],
                    "depends_on": [],
                    "health": None,
                },
                {
                    "name": "db",
                    "image": "postgres:16",
                    "command": None,
                    "env": {"POSTGRES_PASSWORD": "dev"},
                    "ports": [{"container_port": 5432, "protocol": "tcp"}],
                    "volumes": [],
                    "depends_on": [],
                    "health": {
                        "kind": "tcp",
                        "port": 5432,
                        "path": None,
                        "interval_seconds": 5,
                        "timeout_seconds": 3,
                        "retries": 10,
                    },
                },
                {
                    "name": "backend",
                    "image": "ghcr.io/example/api:v1.2.3",
                    "command": None,
                    "env": {},
                    "ports": [],
                    "volumes": [],
                    "depends_on": ["db"],
                    "health": {
                        "kind": "http",
                        "port": 8080,
                        "path": "/healthz",
                        "interval_seconds": 5,
                        "timeout_seconds": 3,
                        "retries": 10,
                    },
                },
            ],
            "initial_state": {"db": {"from": "fixtures/seed.sql"}},
            "resources": {"cpu": "2", "memory": "4Gi"},
        }
        assert actual == expected

    def test_round_trip_preserves_manifest(self) -> None:
        manifest = EnvironmentManifest(
            services=[
                ServiceSpec(name="runner", image="tolokaforge/runner:0.5.0"),
                ServiceSpec(name="db", image="postgres:16", depends_on=[]),
            ],
        )
        rehydrated = EnvironmentManifest.model_validate_json(
            manifest.model_dump_json(by_alias=True)
        )
        assert rehydrated == manifest


# ---------------------------------------------------------------------------
# TaskDescription embedding — pin that the field travels round-trip
# ---------------------------------------------------------------------------


class TestTaskDescriptionEmbedding:
    def test_default_manifest_is_none(self) -> None:
        td = TaskDescription(
            task_id="t1",
            name="x",
            category="general",
            description="x",
            adapter_type="native",
            system_prompt="x",
        )
        assert td.environment_manifest is None

    def test_manifest_round_trip_through_task_description(self) -> None:
        td = TaskDescription(
            task_id="t1",
            name="x",
            category="general",
            description="x",
            adapter_type="native",
            system_prompt="x",
            environment_manifest=_make_minimal_manifest(),
        )
        rehydrated = TaskDescription.model_validate_json(td.model_dump_json(by_alias=True))
        assert rehydrated.environment_manifest == td.environment_manifest
