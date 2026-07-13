"""Pin the ``EnvironmentManifest`` contract — compose-as-source-of-truth.

The manifest is a lean Pydantic wrapper over a Docker Compose file: the
compose file declares service topology (images, ports, volumes, health
probes, depends_on, resources), and the manifest adds engine-specific
fields (runner-service selection, initial-state fixtures, network policy,
security-context defaults) plus safety validators that run against the
loaded compose contents at construction time.

Wire-shape snapshot pins the top-level manifest surface; fixture-driven
tests pin each safety validator against a real compose file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.trial import (
    EnvironmentManifest,
    InitialStateRef,
    NetworkPolicy,
    SecurityContext,
    TaskIsolation,
)
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.canonical


FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


def _fixture(name: str) -> Path:
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture: {path}"
    return path


# ---------------------------------------------------------------------------
# InitialStateRef — retained model
# ---------------------------------------------------------------------------


class TestInitialStateRefContract:
    def test_alias_from_round_trips(self) -> None:
        ref = InitialStateRef.model_validate({"from": "./fixtures/seed.sql", "kind": "sql"})
        assert ref.from_ == "./fixtures/seed.sql"
        dumped = ref.model_dump(by_alias=True)
        assert dumped == {"from": "./fixtures/seed.sql", "kind": "sql"}

    def test_default_kind_is_copy(self) -> None:
        ref = InitialStateRef.model_validate({"from": "./fixtures/blob"})
        assert ref.kind == "copy"

    @pytest.mark.parametrize("kind", ["sql", "copy", "script"])
    def test_all_kinds_are_accepted(self, kind: str) -> None:
        ref = InitialStateRef.model_validate({"from": "./x", "kind": kind})
        assert ref.kind == kind

    def test_empty_from_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": ""})

    @pytest.mark.parametrize("bad", ["../escape", "../../escape", "/etc/passwd"])
    def test_unsafe_from_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": bad})

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            InitialStateRef.model_validate({"from": "./x", "unknown": True})


# ---------------------------------------------------------------------------
# SecurityContext — retained; used by the manifest as security_context_defaults
# ---------------------------------------------------------------------------


class TestSecurityContextContract:
    def test_safer_posture_by_default(self) -> None:
        ctx = SecurityContext()
        assert ctx.no_new_privileges is True
        assert ctx.capabilities_drop == ["ALL"]
        assert ctx.capabilities_add == []
        assert ctx.read_only_root_filesystem is False
        assert ctx.run_as_user is None
        assert ctx.run_as_group is None

    def test_round_trip_preserves_every_field(self) -> None:
        ctx = SecurityContext(
            run_as_user=1000,
            run_as_group=1000,
            read_only_root_filesystem=True,
            no_new_privileges=True,
            capabilities_drop=["ALL"],
            capabilities_add=["NET_BIND_SERVICE"],
        )
        reloaded = SecurityContext.model_validate_json(ctx.model_dump_json())
        assert reloaded == ctx

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SecurityContext.model_validate({"unknown": True})


# ---------------------------------------------------------------------------
# NetworkPolicy — permission-string enum
# ---------------------------------------------------------------------------


class TestNetworkPolicyContract:
    @pytest.mark.parametrize(
        "value",
        [NetworkPolicy.NO_INTERNET, NetworkPolicy.LIMITED_INTERNET, NetworkPolicy.FULL_INTERNET],
    )
    def test_member_values_are_permission_strings(self, value: NetworkPolicy) -> None:
        assert value.value in {"no_internet", "limited_internet", "full_internet"}

    def test_string_construction(self) -> None:
        assert NetworkPolicy("no_internet") is NetworkPolicy.NO_INTERNET
        assert NetworkPolicy("full_internet") is NetworkPolicy.FULL_INTERNET


# ---------------------------------------------------------------------------
# TaskIsolation — per-task isolation-requirement enum
# ---------------------------------------------------------------------------


class TestTaskIsolationContract:
    @pytest.mark.parametrize(
        "value",
        [TaskIsolation.PER_TRIAL, TaskIsolation.SHARED_OK],
    )
    def test_member_values_are_isolation_strings(self, value: TaskIsolation) -> None:
        assert value.value in {"per_trial", "shared_ok"}

    def test_string_construction(self) -> None:
        assert TaskIsolation("per_trial") is TaskIsolation.PER_TRIAL
        assert TaskIsolation("shared_ok") is TaskIsolation.SHARED_OK

    def test_default_on_manifest_is_per_trial(self) -> None:
        """Safety default: a manifest that does not opt out requires
        per-trial isolation. This is the load-bearing invariant that
        prevents silent cross-trial state contamination."""
        m = EnvironmentManifest(compose_file=_fixture("safe_one_service.yaml"))
        assert m.isolation is TaskIsolation.PER_TRIAL

    def test_shared_ok_is_accepted(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_one_service.yaml"),
            isolation=TaskIsolation.SHARED_OK,
        )
        assert m.isolation is TaskIsolation.SHARED_OK

    def test_round_trip_preserves_isolation(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_two_service.yaml"),
            isolation=TaskIsolation.SHARED_OK,
        )
        reloaded = EnvironmentManifest.model_validate_json(m.model_dump_json())
        assert reloaded.isolation is TaskIsolation.SHARED_OK


# ---------------------------------------------------------------------------
# EnvironmentManifest — happy-path construction + defaults
# ---------------------------------------------------------------------------


class TestEnvironmentManifestConstruction:
    def test_minimal_construction_defaults(self) -> None:
        m = EnvironmentManifest(compose_file=_fixture("safe_one_service.yaml"))
        assert m.runner_service == "default"
        assert m.initial_state == {}
        assert m.network_policy is NetworkPolicy.NO_INTERNET
        assert m.security_context_defaults is None

    def test_full_construction_round_trip(self) -> None:
        compose_file = _fixture("safe_two_service.yaml")
        m = EnvironmentManifest(
            compose_file=compose_file,
            runner_service="default",
            initial_state={
                "db": InitialStateRef.model_validate({"from": "./fixtures/seed.sql", "kind": "sql"})
            },
            network_policy=NetworkPolicy.FULL_INTERNET,
            security_context_defaults=SecurityContext(run_as_user=1000),
        )
        reloaded = EnvironmentManifest.model_validate_json(m.model_dump_json())
        assert reloaded == m

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            EnvironmentManifest.model_validate(
                {
                    "compose_file": str(_fixture("safe_one_service.yaml")),
                    "unknown": True,
                }
            )

    def test_load_compose_returns_parsed_dict(self) -> None:
        m = EnvironmentManifest(compose_file=_fixture("safe_two_service.yaml"))
        content = m.load_compose()
        assert set(content["services"]) == {"db", "default"}

    def test_load_compose_returns_cached_snapshot(self, tmp_path: Path) -> None:
        """The manifest snapshots the compose file at construction time.
        Later edits to the file on disk are not reflected — callers see the
        content the safety validators inspected."""
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres:16\n")
        m = EnvironmentManifest(compose_file=compose)
        assert m.load_compose()["services"] == {"default": {"image": "postgres:16"}}
        compose.write_text("services:\n  changed:\n    image: postgres:16\n")
        assert m.load_compose()["services"] == {"default": {"image": "postgres:16"}}


# ---------------------------------------------------------------------------
# EnvironmentManifest — safety validators against loaded compose contents
# ---------------------------------------------------------------------------


class TestComposeSafetyValidators:
    def test_missing_compose_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            EnvironmentManifest(compose_file=tmp_path / "nonexistent.yaml")

    def test_non_mapping_yaml_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- one\n- two\n")
        with pytest.raises(ValidationError, match="YAML mapping"):
            EnvironmentManifest(compose_file=bad)

    def test_missing_services_key_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "no_services.yaml"
        bad.write_text("version: '3'\n")
        with pytest.raises(ValidationError, match="`services:`"):
            EnvironmentManifest(compose_file=bad)

    def test_empty_services_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "empty_services.yaml"
        bad.write_text("services: {}\n")
        with pytest.raises(ValidationError, match="`services:`"):
            EnvironmentManifest(compose_file=bad)

    def test_network_mode_host_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="network_mode: host"):
            EnvironmentManifest(compose_file=_fixture("unsafe_host_network.yaml"))

    def test_privileged_true_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="privileged"):
            EnvironmentManifest(compose_file=_fixture("unsafe_privileged.yaml"))

    def test_cap_add_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cap_add"):
            EnvironmentManifest(compose_file=_fixture("unsafe_cap_add.yaml"))

    def test_bind_mount_with_dotdot_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"\.\."):
            EnvironmentManifest(compose_file=_fixture("unsafe_bind_traversal.yaml"))

    def test_bind_mount_with_absolute_path_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="relative path"):
            EnvironmentManifest(compose_file=_fixture("unsafe_bind_absolute.yaml"))

    def test_bind_mount_with_env_var_expansion_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="shell-expansion"):
            EnvironmentManifest(compose_file=_fixture("unsafe_bind_env_var.yaml"))

    def test_bind_mount_with_tilde_prefix_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="home-directory"):
            EnvironmentManifest(compose_file=_fixture("unsafe_bind_tilde.yaml"))

    def test_privileged_true_string_is_rejected(self, tmp_path: Path) -> None:
        """A quoted `\"true\"` parses as a string but must still fail the
        safety check — otherwise a bare-string edit slips a privileged
        container past the validator."""
        compose = tmp_path / "compose.yaml"
        compose.write_text(
            'services:\n  default:\n    image: postgres:16\n    privileged: "true"\n'
        )
        with pytest.raises(ValidationError, match="privileged"):
            EnvironmentManifest(compose_file=compose)

    def test_privileged_integer_one_is_rejected(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres:16\n    privileged: 1\n")
        with pytest.raises(ValidationError, match="privileged"):
            EnvironmentManifest(compose_file=compose)

    def test_privileged_false_is_accepted(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres:16\n    privileged: false\n")
        EnvironmentManifest(compose_file=compose)  # must not raise

    def test_floating_image_tag_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="floating tag"):
            EnvironmentManifest(compose_file=_fixture("unsafe_floating_tag.yaml"))

    def test_bare_image_reference_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="explicit tag or digest"):
            EnvironmentManifest(compose_file=_fixture("unsafe_bare_image.yaml"))

    @pytest.mark.parametrize(
        "tag",
        ["latest", "main", "master", "edge", "stable", "dev", "develop", "nightly", "head"],
    )
    def test_every_floating_tag_variant_is_rejected(self, tag: str, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text(f"services:\n  default:\n    image: postgres:{tag}\n")
        with pytest.raises(ValidationError, match="floating tag"):
            EnvironmentManifest(compose_file=compose)

    def test_sha256_digest_reference_is_accepted(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        digest = "sha256:" + "a" * 64
        compose.write_text(f"services:\n  default:\n    image: postgres@{digest}\n")
        EnvironmentManifest(compose_file=compose)  # must not raise

    def test_malformed_digest_is_rejected(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres@sha256:short\n")
        with pytest.raises(ValidationError, match="digest"):
            EnvironmentManifest(compose_file=compose)

    def test_build_only_service_is_accepted(self, tmp_path: Path) -> None:
        """A service that declares `build:` instead of `image:` is exempt
        from the pinning check — no tag to pin."""
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    build: ./ctx\n")
        EnvironmentManifest(compose_file=compose)  # must not raise

    def test_depends_on_undeclared_service_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not declared in the compose file"):
            EnvironmentManifest(compose_file=_fixture("unsafe_missing_depends_on.yaml"))

    def test_depends_on_long_form_undeclared_service_is_rejected(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text(
            "services:\n"
            "  default:\n"
            "    image: tolokaforge/runner:0.5.0\n"
            "    depends_on:\n"
            "      db_typo:\n"
            "        condition: service_healthy\n"
        )
        with pytest.raises(ValidationError, match="not declared in the compose file"):
            EnvironmentManifest(compose_file=compose)

    def test_depends_on_scalar_is_rejected(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres:16\n    depends_on: 5\n")
        with pytest.raises(ValidationError, match="must be a list or a mapping"):
            EnvironmentManifest(compose_file=compose)

    def test_volumes_scalar_is_rejected(self, tmp_path: Path) -> None:
        """A non-list `volumes:` value must fail explicitly rather than be
        silently swallowed as empty."""
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  default:\n    image: postgres:16\n    volumes: ''\n")
        with pytest.raises(ValidationError, match="must be a list"):
            EnvironmentManifest(compose_file=compose)

    def test_named_volume_reference_is_accepted(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text(
            "volumes:\n  data: {}\nservices:\n  default:\n"
            "    image: postgres:16\n    volumes:\n      - data:/var/lib/data\n"
        )
        m = EnvironmentManifest(compose_file=compose)
        assert m.load_compose()["services"]["default"]["volumes"] == ["data:/var/lib/data"]

    def test_long_form_bind_mount_is_validated(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yaml"
        compose.write_text(
            "services:\n  default:\n    image: postgres:16\n"
            "    volumes:\n      - type: bind\n        source: ../escape\n"
            "        target: /data\n"
        )
        with pytest.raises(ValidationError, match=r"\.\."):
            EnvironmentManifest(compose_file=compose)


# ---------------------------------------------------------------------------
# EnvironmentManifest — cross-field validation
# ---------------------------------------------------------------------------


class TestManifestCrossFieldValidation:
    def test_runner_service_must_exist_in_compose(self) -> None:
        with pytest.raises(ValidationError, match="not declared in the compose file"):
            EnvironmentManifest(
                compose_file=_fixture("safe_one_service.yaml"),
                runner_service="does_not_exist",
            )

    def test_initial_state_keys_must_match_declared_services(self) -> None:
        with pytest.raises(ValidationError, match="does not match any declared service"):
            EnvironmentManifest(
                compose_file=_fixture("safe_two_service.yaml"),
                initial_state={
                    "not_a_service": InitialStateRef.model_validate(
                        {"from": "./fixtures/seed.sql"}
                    ),
                },
            )

    def test_initial_state_keys_are_accepted_when_they_match(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_two_service.yaml"),
            initial_state={
                "db": InitialStateRef.model_validate({"from": "./fixtures/seed.sql", "kind": "sql"})
            },
        )
        assert "db" in m.initial_state


# ---------------------------------------------------------------------------
# Wire-shape snapshot — top-level manifest fields
# ---------------------------------------------------------------------------


class TestManifestWireShape:
    def test_canonical_wire_shape(self) -> None:
        """Lock the JSON shape callers see over the wire. Adding, removing, or
        renaming a top-level manifest field must update this test — that is
        what makes it canonical."""
        m = EnvironmentManifest(
            compose_file=_fixture("safe_two_service.yaml"),
            runner_service="default",
            initial_state={
                "db": InitialStateRef.model_validate({"from": "./fixtures/seed.sql", "kind": "sql"})
            },
            network_policy=NetworkPolicy.NO_INTERNET,
            security_context_defaults=SecurityContext(),
        )
        wire = m.model_dump(mode="json")
        assert set(wire) == {
            "compose_file",
            "runner_service",
            "stack_inputs",
            "initial_state",
            "network_policy",
            "security_context_defaults",
            "isolation",
        }
        assert wire["runner_service"] == "default"
        assert wire["network_policy"] == "no_internet"
        assert wire["isolation"] == "per_trial"
        assert wire["initial_state"] == {"db": {"from_": "./fixtures/seed.sql", "kind": "sql"}}
        assert wire["security_context_defaults"] == {
            "run_as_user": None,
            "run_as_group": None,
            "read_only_root_filesystem": False,
            "no_new_privileges": True,
            "capabilities_drop": ["ALL"],
            "capabilities_add": [],
        }


# ---------------------------------------------------------------------------
# TaskDescription embedding — optional, defaults to None
# ---------------------------------------------------------------------------


class TestTaskDescriptionEmbedding:
    def _task(self, manifest: EnvironmentManifest | None = None) -> TaskDescription:
        return TaskDescription(
            task_id="t1",
            name="t1",
            category="test",
            description="d",
            adapter_type="native",
            system_prompt="",
            environment_manifest=manifest,
        )

    def test_manifest_defaults_to_none(self) -> None:
        assert self._task().environment_manifest is None

    def test_manifest_round_trips_through_task_description(self) -> None:
        m = EnvironmentManifest(compose_file=_fixture("safe_two_service.yaml"))
        task = self._task(m)
        reloaded = TaskDescription.model_validate_json(task.model_dump_json())
        assert reloaded.environment_manifest is not None
        assert reloaded.environment_manifest.compose_file == m.compose_file
        assert reloaded.environment_manifest.runner_service == "default"


def test_fixtures_are_parseable() -> None:
    """Sanity: pyyaml can parse every fixture, and each has a services mapping.
    Guards against accidental fixture drift outside the safety-validator tests.
    """
    for path in FIXTURES.iterdir():
        if path.suffix != ".yaml":
            continue
        with path.open() as f:
            content = yaml.safe_load(f)
        assert isinstance(content, dict), f"{path.name}: not a mapping"
        assert isinstance(content.get("services"), dict), f"{path.name}: no services mapping"
