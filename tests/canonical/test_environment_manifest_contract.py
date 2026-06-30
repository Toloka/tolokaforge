"""Pin the JSON wire shape and validators of ``EnvironmentManifest``.

The manifest is the typed declaration of a task's multi-service environment;
this module pins every field, every default, and every cross-field validator
so a silent change to either fails CI.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.runner.models import (
    DependsOn,
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
    def test_tcp_probe_defaults(self) -> None:
        probe = HealthProbe(kind="tcp", port=5432)
        assert probe.path is None
        assert probe.initial_delay_seconds == 0
        assert probe.interval_seconds == 5
        assert probe.timeout_seconds == 3
        assert probe.retries == 10

    def test_http_probe_requires_path(self) -> None:
        with pytest.raises(ValidationError, match="path is required"):
            HealthProbe(kind="http", port=8080)

    def test_tcp_probe_rejects_path(self) -> None:
        with pytest.raises(ValidationError, match="not allowed"):
            HealthProbe(kind="tcp", port=5432, path="/healthz")

    def test_initial_delay_seconds_round_trip(self) -> None:
        probe = HealthProbe(
            kind="http",
            port=8080,
            path="/healthz",
            initial_delay_seconds=30,
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
    def test_minimal_mount_defaults_to_bind(self) -> None:
        mount = VolumeMount(source="fixtures/seed.sql", target="/seed.sql")
        assert mount.kind == "bind"
        assert mount.read_only is False

    def test_named_volume_round_trip(self) -> None:
        mount = VolumeMount(kind="named", source="pgdata", target="/var/lib/postgresql/data")
        assert VolumeMount.model_validate_json(mount.model_dump_json()) == mount

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VolumeMount.model_validate({"kind": "tmpfs", "source": "a", "target": "/b"})

    @pytest.mark.parametrize(
        "bad_source",
        ["..", "../escape", "a/../b", "/etc/passwd", "/", ""],
    )
    def test_bind_source_rejects_unsafe_paths(self, bad_source: str) -> None:
        with pytest.raises(ValidationError):
            VolumeMount(kind="bind", source=bad_source, target="/in/container")

    def test_named_source_skips_path_traversal_check(self) -> None:
        # Named volume identifiers are not paths; path-traversal guard does not apply.
        mount = VolumeMount(kind="named", source="../pgdata", target="/data")
        assert mount.kind == "named"
        assert mount.source == "../pgdata"

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
    def test_round_trip_via_alias_defaults_to_copy(self) -> None:
        ref = InitialStateRef.model_validate({"from": "fixtures/orders.sql"})
        assert ref.from_ == "fixtures/orders.sql"
        assert ref.kind == "copy"
        dumped = ref.model_dump(by_alias=True)
        assert dumped == {"from": "fixtures/orders.sql", "kind": "copy"}

    @pytest.mark.parametrize("kind", ["sql", "copy", "script"])
    def test_all_kinds_accepted(self, kind: str) -> None:
        ref = InitialStateRef.model_validate({"from": "x", "kind": kind})
        assert ref.kind == kind

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": "x", "kind": "exec"})

    def test_empty_from_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            InitialStateRef.model_validate({"from": ""})

    @pytest.mark.parametrize(
        "bad_from",
        ["..", "../escape.sql", "a/../b.sql", "/etc/passwd", "/"],
    )
    def test_from_rejects_unsafe_paths(self, bad_from: str) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": bad_from})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": "a.sql", "format": "sql"})


# ---------------------------------------------------------------------------
# DependsOn
# ---------------------------------------------------------------------------


class TestDependsOnContract:
    def test_minimal_construction_defaults_to_started(self) -> None:
        dep = DependsOn(service="db")
        assert dep.condition == "service_started"

    def test_service_healthy_accepted(self) -> None:
        dep = DependsOn(service="db", condition="service_healthy")
        assert dep.condition == "service_healthy"

    def test_unknown_condition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DependsOn.model_validate({"service": "db", "condition": "service_completed"})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DependsOn.model_validate({"service": "db", "restart": True})


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
        assert svc.resources is None

    def test_full_construction_round_trip(self) -> None:
        svc = ServiceSpec(
            name="db",
            image="postgres:16",
            command=["postgres", "-c", "shared_buffers=256MB"],
            env={"POSTGRES_PASSWORD": "dev"},
            ports=[PortSpec(container_port=5432)],
            volumes=[VolumeMount(kind="named", source="pgdata", target="/var/lib/postgresql/data")],
            depends_on=[],
            health=HealthProbe(kind="tcp", port=5432, initial_delay_seconds=15),
            resources=Resources(cpu="2", memory="4Gi"),
        )
        assert ServiceSpec.model_validate_json(svc.model_dump_json()) == svc

    def test_per_service_resources_override(self) -> None:
        svc = ServiceSpec(
            name="runner",
            image="tolokaforge/runner:0.5.0",
            resources=Resources(cpu="500m", memory="256Mi"),
        )
        assert svc.resources is not None
        assert svc.resources.cpu == "500m"

    def test_depends_on_accepts_string_shorthand(self) -> None:
        svc = ServiceSpec(
            name="backend",
            image="ghcr.io/example/api:v1.2.3",
            depends_on=["db"],
        )
        # String entries stay as strings on the wire (shorthand for service_started).
        assert svc.depends_on == ["db"]

    def test_depends_on_accepts_structured_form(self) -> None:
        svc = ServiceSpec(
            name="backend",
            image="ghcr.io/example/api:v1.2.3",
            depends_on=[DependsOn(service="db", condition="service_healthy")],
        )
        assert isinstance(svc.depends_on[0], DependsOn)
        assert svc.depends_on[0].condition == "service_healthy"

    def test_depends_on_mixed_round_trip(self) -> None:
        svc = ServiceSpec(
            name="backend",
            image="ghcr.io/example/api:v1.2.3",
            depends_on=["db", DependsOn(service="cache", condition="service_healthy")],
        )
        rehydrated = ServiceSpec.model_validate_json(svc.model_dump_json())
        assert rehydrated == svc

    # ---- image-pinning ----

    def test_latest_tag_rejected(self) -> None:
        with pytest.raises(ValidationError, match="floating tag"):
            ServiceSpec(name="runner", image="tolokaforge/runner:latest")

    def test_latest_tag_case_insensitive(self) -> None:
        with pytest.raises(ValidationError, match="floating tag"):
            ServiceSpec(name="runner", image="tolokaforge/runner:LATEST")

    @pytest.mark.parametrize("tag", ["main", "master", "edge", "stable", "dev", "nightly", "head"])
    def test_other_floating_tags_rejected(self, tag: str) -> None:
        with pytest.raises(ValidationError, match="floating tag"):
            ServiceSpec(name="svc", image=f"example/svc:{tag}")

    def test_bare_image_rejected(self) -> None:
        with pytest.raises(ValidationError, match="explicit tag or digest"):
            ServiceSpec(name="runner", image="tolokaforge/runner")

    def test_registry_with_port_but_no_tag_rejected(self) -> None:
        with pytest.raises(ValidationError, match="explicit tag or digest"):
            ServiceSpec(name="runner", image="registry.example.com:5000/foo")

    def test_registry_with_port_and_tag_accepted(self) -> None:
        svc = ServiceSpec(name="runner", image="registry.example.com:5000/foo:1.2.3")
        assert svc.image == "registry.example.com:5000/foo:1.2.3"

    def test_empty_tag_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            ServiceSpec(name="runner", image="example/svc:")

    def test_sha_digest_accepted(self) -> None:
        svc = ServiceSpec(
            name="runner",
            image="tolokaforge/runner@sha256:" + "0" * 64,
        )
        assert svc.image.startswith("tolokaforge/runner@sha256:")

    def test_digest_with_tag_accepted(self) -> None:
        # Docker permits tag + digest; digest takes precedence and is always pinned.
        svc = ServiceSpec(
            name="runner",
            image="example/svc:1.2.3@sha256:" + "0" * 64,
        )
        assert "@sha256:" in svc.image

    def test_sha512_digest_accepted(self) -> None:
        svc = ServiceSpec(name="runner", image="example/svc@sha512:" + "0" * 128)
        assert "@sha512:" in svc.image

    @pytest.mark.parametrize(
        "bad_image",
        [
            "repo@sha256:zzz",  # not hex
            "repo@sha256:" + "0" * 63,  # one char short
            "repo@sha256:" + "0" * 65,  # one char long
            "repo@sha512:" + "0" * 64,  # sha512 must be 128 hex
            "repo@md5:" + "0" * 32,  # unsupported algorithm
            "repo@:" + "0" * 64,  # missing algorithm
            "repo@sha256:",  # empty digest
        ],
    )
    def test_malformed_digest_rejected(self, bad_image: str) -> None:
        with pytest.raises(ValidationError, match="well-formed"):
            ServiceSpec(name="runner", image=bad_image)

    # ---- name validation ----

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
        with pytest.raises(ValidationError):
            ServiceSpec(name=bad_name, image="tolokaforge/runner:0.5.0")

    def test_dns_label_length_cap_63(self) -> None:
        ok = "a" + "b" * 62  # 63 chars total
        ServiceSpec(name=ok, image="example/svc:1.0")
        too_long = "a" + "b" * 63  # 64 chars
        with pytest.raises(ValidationError, match="at most 63 characters"):
            ServiceSpec(name=too_long, image="example/svc:1.0")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServiceSpec.model_validate(
                {"name": "runner", "image": "tolokaforge/runner:0.5.0", "restart": "always"}
            )


# ---------------------------------------------------------------------------
# EnvironmentManifest — cross-service validators + wire shape
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

    def test_depends_on_string_must_resolve(self) -> None:
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

    def test_depends_on_dependson_form_must_resolve(self) -> None:
        with pytest.raises(ValidationError, match="unknown service"):
            EnvironmentManifest(
                services=[
                    ServiceSpec(
                        name="runner",
                        image="tolokaforge/runner:0.5.0",
                        depends_on=[
                            DependsOn(service="ghost", condition="service_healthy"),
                        ],
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

    # ---- network mode (safety default) ----

    def test_network_defaults_to_isolated(self) -> None:
        manifest = _make_minimal_manifest()
        assert manifest.network == "isolated"

    @pytest.mark.parametrize("mode", ["isolated", "external"])
    def test_network_modes_accepted(self, mode: str) -> None:
        manifest = EnvironmentManifest(services=[_make_minimal_service()], network=mode)
        assert manifest.network == mode

    def test_unknown_network_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EnvironmentManifest.model_validate(
                {
                    "services": [{"name": "runner", "image": "tolokaforge/runner:0.5.0"}],
                    "network": "bridge",
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
                    health=HealthProbe(kind="tcp", port=5432, initial_delay_seconds=15),
                    resources=Resources(cpu="2", memory="2Gi"),
                ),
                ServiceSpec(
                    name="backend",
                    image="ghcr.io/example/api:v1.2.3",
                    depends_on=["db", DependsOn(service="cache", condition="service_healthy")],
                    health=HealthProbe(kind="http", port=8080, path="/healthz"),
                ),
                ServiceSpec(name="cache", image="redis:7"),
            ],
            initial_state={
                "db": InitialStateRef.model_validate({"from": "fixtures/seed.sql", "kind": "sql"}),
            },
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
                    "resources": None,
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
                        "initial_delay_seconds": 15,
                        "interval_seconds": 5,
                        "timeout_seconds": 3,
                        "retries": 10,
                    },
                    "resources": {"cpu": "2", "memory": "2Gi"},
                },
                {
                    "name": "backend",
                    "image": "ghcr.io/example/api:v1.2.3",
                    "command": None,
                    "env": {},
                    "ports": [],
                    "volumes": [],
                    "depends_on": [
                        "db",
                        {"service": "cache", "condition": "service_healthy"},
                    ],
                    "health": {
                        "kind": "http",
                        "port": 8080,
                        "path": "/healthz",
                        "initial_delay_seconds": 0,
                        "interval_seconds": 5,
                        "timeout_seconds": 3,
                        "retries": 10,
                    },
                    "resources": None,
                },
                {
                    "name": "cache",
                    "image": "redis:7",
                    "command": None,
                    "env": {},
                    "ports": [],
                    "volumes": [],
                    "depends_on": [],
                    "health": None,
                    "resources": None,
                },
            ],
            "initial_state": {"db": {"from": "fixtures/seed.sql", "kind": "sql"}},
            "resources": {"cpu": "2", "memory": "4Gi"},
            "network": "isolated",
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
# TaskDescription embedding
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
