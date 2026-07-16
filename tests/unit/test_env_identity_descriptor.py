"""Resolved environment-identity descriptor.

:func:`describe_environment_identity` turns a resolved
:class:`EnvironmentManifest` into a human-readable
:class:`EnvironmentIdentity`: per-service resolved images (pinned or
floating), isolation, reset seeds, redacted connection strings, and
container mount targets, plus the run-level network policy and
runner-service pointer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.env_identity import describe_environment_identity
from tolokaforge.core.models import ResetSpec, ServiceSpec
from tolokaforge.core.trial import EnvironmentManifest, NetworkPolicy

pytestmark = pytest.mark.unit

_COMPOSE = """\
services:
  runner:
    image: tolokaforge-runner:0.5.0
    environment:
      DB_SERVICE_URL: "http://db-service:8000"
  app-service:
    image: ${APP_IMAGE:-postgres:16}
    environment:
      APP_DB_DSN: "postgresql://app:app_pw@app-db:5432/mfg"
    volumes:
      - ./app/main.py:/srv/app/main.py:ro
  app-db:
    image: postgres:16
    environment:
      POSTGRES_DB: "mfg"
      POSTGRES_USER: "app"
      POSTGRES_PASSWORD: "app_pw"
    volumes:
      - ./app-db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
"""


def _manifest(tmp_path: Path, **overrides) -> EnvironmentManifest:
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(_COMPOSE)
    kwargs = {
        "compose_file": compose_file,
        "runner_service": "runner",
        "services": {
            "runner": ServiceSpec(isolation="shared"),
            "app-service": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
        },
    }
    kwargs.update(overrides)
    return EnvironmentManifest(**kwargs)


class TestImages:
    def test_literal_pinned_tag(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        app_db = identity.services["app-db"]
        assert app_db.image == "postgres:16"
        assert app_db.pinned is True

    def test_resolved_floating_tag_is_not_pinned(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(
            _manifest(tmp_path, stack_inputs={"APP_IMAGE": "postgres:latest"})
        )
        app_service = identity.services["app-service"]
        assert app_service.image == "postgres:latest"
        assert app_service.pinned is False

    def test_var_image_resolves_from_stack_inputs(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(
            _manifest(tmp_path, stack_inputs={"APP_IMAGE": "registry.example/app:2.0"})
        )
        app_service = identity.services["app-service"]
        assert app_service.image == "registry.example/app:2.0"
        assert app_service.pinned is True

    def test_var_image_falls_back_to_default(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        app_service = identity.services["app-service"]
        assert app_service.image == "postgres:16"
        assert app_service.pinned is True


class TestNetworkPolicy:
    def test_defaults_to_no_internet(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        assert identity.network_policy == "no_internet"

    def test_reflects_set_value(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(
            _manifest(tmp_path, network_policy=NetworkPolicy.FULL_INTERNET)
        )
        assert identity.network_policy == "full_internet"


class TestDsnRedaction:
    def test_password_redacted_components_preserved(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        assert identity.services["app-service"].dsns == ["postgresql://app:***@app-db:5432/mfg"]

    def test_secret_never_appears(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        assert "app_pw" not in identity.model_dump_json()

    def test_plaintext_secret_env_is_not_a_dsn(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        assert identity.services["app-db"].dsns == []


_QUERY_PW_COMPOSE = """\
services:
  runner:
    image: tolokaforge-runner:0.5.0
  app-service:
    image: postgres:16
    environment:
      QUERY_DSN: "postgresql://app@app-db:5432/mfg?password=app_pw&sslmode=require"
      BOTH_DSN: "postgresql://app:app_pw@app-db:5432/mfg?sslpassword=app_pw"
      CLEAN_DSN: "postgresql://app@app-db:5432/mfg?sslmode=require"
"""


def _query_pw_manifest(tmp_path: Path) -> EnvironmentManifest:
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(_QUERY_PW_COMPOSE)
    return EnvironmentManifest(
        compose_file=compose_file,
        runner_service="runner",
        services={"runner": ServiceSpec(isolation="shared")},
    )


class TestDsnQueryPasswordRedaction:
    def test_query_string_password_redacted_others_preserved(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_query_pw_manifest(tmp_path))
        dsns = identity.services["app-service"].dsns
        assert dsns == [
            "postgresql://app:***@app-db:5432/mfg?sslpassword=***",
            "postgresql://app@app-db:5432/mfg?password=***&sslmode=require",
            "postgresql://app@app-db:5432/mfg?sslmode=require",
        ]

    def test_query_string_secret_never_appears(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_query_pw_manifest(tmp_path))
        assert "app_pw" not in identity.model_dump_json()


class TestMounts:
    def test_target_and_mode_recorded_host_source_excluded(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        mounts = identity.services["app-service"].mounts
        assert mounts == ["/srv/app/main.py:ro"]
        assert "./app/main.py" not in identity.model_dump_json()


class TestIsolation:
    def test_service_absent_from_manifest_defaults_to_ephemeral(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        assert identity.services["app-db"].isolation == "ephemeral"
        assert identity.services["app-db"].reset_seed is None

    def test_reset_seed_recorded(self, tmp_path: Path) -> None:
        identity = describe_environment_identity(_manifest(tmp_path))
        app_service = identity.services["app-service"]
        assert app_service.isolation == "reset"
        assert app_service.reset_seed == "baseline"
