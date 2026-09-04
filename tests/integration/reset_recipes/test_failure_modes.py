"""Real-container failure-mode tests for the reset recipes.

Each recipe has a fail-loud contract: on its named failure input it
raises :class:`RuntimeError` naming the service and carrying the failing
command's diagnostic output. These tests boot a real stack per recipe and
assert that contract against genuine Docker behaviour (bad SQL, a
non-directory seed, a corrupt RDB), then prove that a recipe failure
inside :meth:`PerTrialRuntimeBackend.provision` surfaces as
``ProvisionError(stage="reset_recipe")`` and tears the stack down with no
leaked containers or networks.

``bare`` has no failure case: it performs no container-side action (see
``test_bare_recipe.py``), so there is nothing for a caller to break.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from testcontainers.compose import DockerCompose

from tests.canonical._factories import make_task_description
from tolokaforge.core.models import ModelConfig, SeedRef
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ResetSpec, ServiceSpec
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY, redis_dump

pytestmark = [pytest.mark.integration, pytest.mark.docker]


POSTGRES_COMPOSE = textwrap.dedent("""
    services:
      postgres:
        image: "postgres:16-alpine"
        environment:
          POSTGRES_USER: "postgres"
          POSTGRES_PASSWORD: "example"
          POSTGRES_DB: "app"
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
          interval: 2s
          timeout: 3s
          retries: 30
    """).strip()


REDIS_COMPOSE = textwrap.dedent("""
    services:
      redis:
        image: "redis:7-alpine"
        command: ["redis-server", "--save", "", "--appendonly", "no"]
        healthcheck:
          test: ["CMD", "redis-cli", "PING"]
          interval: 2s
          timeout: 3s
          retries: 30
    """).strip()


ALPINE_COMPOSE = textwrap.dedent("""
    services:
      workspace:
        image: "alpine:3.20"
        command: ["sh", "-c", "mkdir -p /workspace && sleep 3600"]
        healthcheck:
          test: ["CMD", "test", "-d", "/workspace"]
          interval: 2s
          timeout: 3s
          retries: 30
    """).strip()


BROKEN_SQL = "CREATE TABLE widgets (id INT PRIMARY KEY;\nTHIS IS NOT VALID SQL;\n"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_compose(tmp_path: Path, compose_text: str) -> Path:
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(compose_text + "\n")
    return compose_dir


def _boot(compose_dir: Path) -> DockerCompose:
    return DockerCompose(
        context=str(compose_dir),
        compose_file_name="docker-compose.yml",
        pull=True,
        build=False,
        wait=True,
    )


class TestSqlDumpFailure:
    def test_bad_sql_raises_runtime_error_with_service_and_stderr(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "broken.sql"
        seed_file.write_text(BROKEN_SQL)
        seed = SeedRef(path=seed_file, kind="sql_dump", digest=_digest(seed_file.read_bytes()))

        with _boot(_write_compose(tmp_path, POSTGRES_COMPOSE)) as compose:
            with pytest.raises(RuntimeError) as exc:
                RECIPE_REGISTRY["sql_dump"].apply(seed, "postgres", compose)

        message = str(exc.value)
        assert "'postgres'" in message
        assert str(seed_file) in message
        assert "rc=0" not in message
        assert "syntax error" in message


class TestFilesystemDirFailure:
    def test_non_directory_seed_raises_before_any_container_action(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "not_a_dir.txt"
        seed_file.write_text("this is a file, not a directory tree")
        seed = SeedRef(
            path=seed_file, kind="filesystem_dir", digest=_digest(seed_file.read_bytes())
        )

        with _boot(_write_compose(tmp_path, ALPINE_COMPOSE)) as compose:
            # The is_dir() guard precedes any Docker call; a live stack is
            # here only to prove no container mutation is attempted.
            with patch.object(subprocess, "run") as run_mock:
                with pytest.raises(RuntimeError) as exc:
                    RECIPE_REGISTRY["filesystem_dir"].apply(seed, "workspace", compose)
            assert run_mock.call_count == 0

        message = str(exc.value)
        assert str(seed_file) in message
        assert "not a directory" in message


class TestRedisDumpFailure:
    def test_corrupt_rdb_crash_loops_and_fails_ping_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-empty garbage: Redis rejects the RDB signature at startup and
        # exits, so the restarted process never accepts PING. (An *empty*
        # file is a valid empty DB and would load cleanly.)
        rdb = tmp_path / "dump.rdb"
        rdb.write_bytes(b"not-a-valid-redis-rdb-file\x00\xff\x01\x02")
        seed = SeedRef(path=rdb, kind="redis_dump", digest=_digest(rdb.read_bytes()))

        # Patch the module constant (not behaviour) so the crash-loop is
        # detected in a few seconds instead of the 30 s production poll.
        monkeypatch.setattr(redis_dump, "RESTART_PING_MAX_ATTEMPTS", 3)

        with _boot(_write_compose(tmp_path, REDIS_COMPOSE)) as compose:
            with pytest.raises(RuntimeError) as exc:
                RECIPE_REGISTRY["redis_dump"].apply(seed, "redis", compose)

        message = str(exc.value)
        assert "'redis'" in message
        assert "ping stage" in message


class TestProvisionTearsDownCleanlyOnRecipeFailure:
    """A real recipe failure inside ``provision`` must attribute to
    ``stage="reset_recipe"`` and leave no orphan containers or networks —
    ``cleanup_partial_materialisation`` brings the whole stack down."""

    def test_broken_seed_yields_reset_recipe_stage_and_clean_teardown(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path, POSTGRES_COMPOSE) / "docker-compose.yml"
        seed_file = tmp_path / "broken.sql"
        seed_file.write_text(BROKEN_SQL)

        manifest = EnvironmentManifest(
            compose_file=compose_file,
            runner_service="postgres",
            services={"postgres": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline"))},
        )
        backend = PerTrialRuntimeBackend(
            seeds={
                "baseline": SeedRef(
                    path=seed_file, kind="sql_dump", digest=_digest(seed_file.read_bytes())
                )
            }
        )
        spec = TrialSpec(
            trial_id="reset-failure:0",
            run_id="reset-recipe-failure-teardown",
            task=make_task_description(
                task_id="task-1",
                name="probe",
                category="general",
                description="reset-recipe failure teardown test",
                environment_manifest=manifest,
            ),
            agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
            env_endpoints=EnvEndpoints(
                db_url="http://placeholder:5432",
                runner_url="http://placeholder:50051",
            ),
        )

        before_containers = _docker_container_ids()
        before_networks = _docker_network_ids()

        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)

        assert exc.value.stage == "reset_recipe"
        assert "'postgres'" in exc.value.reason
        assert "reset recipe" in exc.value.reason

        # No new container or network survived the failed provision.
        assert _docker_container_ids() <= before_containers
        assert _docker_network_ids() <= before_networks
        assert spec.trial_id not in backend._delegate._env_handles


def _docker_container_ids() -> set[str]:
    result = subprocess.run(["docker", "ps", "-aq"], capture_output=True, check=True, text=True)
    return set(result.stdout.split())


def _docker_network_ids() -> set[str]:
    result = subprocess.run(
        ["docker", "network", "ls", "-q"], capture_output=True, check=True, text=True
    )
    return set(result.stdout.split())
