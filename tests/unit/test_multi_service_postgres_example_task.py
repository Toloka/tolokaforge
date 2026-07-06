"""Static-shape tests for the ``support_triage_01`` task pack.

The task pack under ``examples/native/multi_service_postgres/`` is the
realistic-backend demonstration workload for Case B (ADR-0018):
a shared-runtime task-declared compose stack that runs a real
postgres:16 database plus a PostgREST HTTP API on top of it. Real
docker execution lives in the docker integration suite; here we pin
the task pack's static shape + the fixture aggregation so a future
refactor doesn't silently break the example.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.trial import EnvironmentManifest, TaskIsolation

pytestmark = pytest.mark.unit


_TASK_DIR = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "native"
    / "multi_service_postgres"
    / "dataset"
    / "tasks"
    / "multi_service"
    / "support_triage_01"
)


class TestTaskLayoutOnDisk:
    def test_task_yaml_exists(self) -> None:
        assert (_TASK_DIR / "task.yaml").is_file()

    def test_compose_file_exists(self) -> None:
        assert (_TASK_DIR / "environment.compose.yaml").is_file()

    def test_grading_file_exists(self) -> None:
        assert (_TASK_DIR / "grading.yaml").is_file()

    def test_init_sql_exists(self) -> None:
        """The compose file bind-mounts ``app-db/init.sql`` into
        postgres's ``docker-entrypoint-initdb.d`` — the file must exist
        or postgres starts with an empty database and every query
        returns 0 rows."""
        assert (_TASK_DIR / "app-db" / "init.sql").is_file()


class TestTaskYaml:
    def test_loads_via_task_loader(self) -> None:
        task_config, task_root = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.task_id == "support_triage_01"
        assert task_root == _TASK_DIR

    def test_declares_environment_manifest(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest is not None
        assert isinstance(task_config.environment_manifest, EnvironmentManifest)

    def test_isolation_is_shared_ok(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest.isolation == TaskIsolation.SHARED_OK

    def test_runner_service_matches_compose_declaration(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        compose_body = yaml.safe_load(task_config.environment_manifest.compose_file.read_text())
        declared_services = set(compose_body.get("services", {}).keys())
        assert task_config.environment_manifest.runner_service in declared_services

    def test_agent_tools_reach_api(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        enabled = set(task_config.tools.agent.get("enabled", []))
        assert "bash" in enabled
        assert "write_file" in enabled


class TestComposeShape:
    """The compose file must declare four services: runner + db-service
    (engine state backend) + app-service (PostgREST) + app-db (real
    postgres)."""

    def _load_compose(self) -> dict:
        return yaml.safe_load((_TASK_DIR / "environment.compose.yaml").read_text())

    def test_declares_four_services(self) -> None:
        compose = self._load_compose()
        assert set(compose["services"].keys()) == {
            "runner",
            "db-service",
            "app-service",
            "app-db",
        }

    def test_runner_uses_local_alias(self) -> None:
        compose = self._load_compose()
        assert compose["services"]["runner"]["image"] == "tolokaforge-runner:local"

    def test_db_service_uses_local_alias(self) -> None:
        compose = self._load_compose()
        assert compose["services"]["db-service"]["image"] == "tolokaforge-db-service:local"

    def test_app_service_is_pinned_postgrest(self) -> None:
        compose = self._load_compose()
        image = compose["services"]["app-service"]["image"]
        assert image.startswith("postgrest/postgrest:")
        tag = image.split(":", 1)[1]
        assert tag not in {"latest", "edge", "stable", "main", "master"}

    def test_app_db_is_pinned_postgres(self) -> None:
        compose = self._load_compose()
        image = compose["services"]["app-db"]["image"]
        assert image.startswith("postgres:")
        tag = image.split(":", 1)[1]
        assert tag not in {"latest", "edge", "stable", "main", "master"}

    def test_app_service_connects_to_app_db(self) -> None:
        """PostgREST's PGRST_DB_URI must reference the compose service
        name of the postgres instance — otherwise DNS-resolution fails
        at container start and postgrest crashes."""
        compose = self._load_compose()
        db_uri = compose["services"]["app-service"]["environment"]["PGRST_DB_URI"]
        assert "@app-db:5432" in db_uri

    def test_app_service_depends_on_healthy_db(self) -> None:
        """PostgREST connects to postgres on startup — postgres must be
        healthy first, otherwise postgrest crashes on init."""
        compose = self._load_compose()
        depends = compose["services"]["app-service"]["depends_on"]
        assert depends["app-db"]["condition"] == "service_healthy"

    def test_init_sql_bind_mounted_read_only(self) -> None:
        """The seed SQL script must land in postgres's init-scripts
        directory to run at first-start. Read-only mount so a
        misbehaving container can't rewrite the seed."""
        compose = self._load_compose()
        mounts = compose["services"]["app-db"]["volumes"]
        assert any(
            m.startswith("./app-db/init.sql:") and m.endswith(":ro") for m in mounts
        ), f"expected init.sql read-only bind mount into postgres, got {mounts!r}"


class TestInitSqlSeed:
    """The task's grading pins the top-3 open enterprise-tier tickets.
    If the seed data ever changes, the grading breaks silently (agent
    gets 0/6 on state_checks). Pin the aggregation up-front."""

    def _load_sql(self) -> str:
        return (_TASK_DIR / "app-db" / "init.sql").read_text()

    def _parse_customers(self, sql: str) -> dict[str, dict]:
        """Extract the INSERT INTO api.customers rows into a
        {customer_id: {name, tier}} dict. Regex-based parsing — we're
        not asking postgres to run this in a test, we just need to
        confirm the seed's shape."""
        m = re.search(
            r"INSERT INTO api\.customers\s*\([^)]+\)\s*VALUES\s*(.+?);",
            sql,
            re.DOTALL,
        )
        assert m is not None, "customers INSERT not found in init.sql"
        block = m.group(1)
        row_re = re.compile(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
        return {cid: {"name": name, "tier": tier} for cid, name, tier in row_re.findall(block)}

    def _parse_tickets(self, sql: str) -> list[dict]:
        m = re.search(
            r"INSERT INTO api\.tickets\s*\([^)]+\)\s*VALUES\s*(.+?);",
            sql,
            re.DOTALL,
        )
        assert m is not None, "tickets INSERT not found in init.sql"
        block = m.group(1)
        row_re = re.compile(
            r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*(\d+)\s*,\s*'([^']+)'\s*\)"
        )
        return [
            {
                "ticket_id": tid,
                "customer_id": cid,
                "status": status,
                "priority": int(priority),
                "subject": subject,
            }
            for tid, cid, status, priority, subject in row_re.findall(block)
        ]

    def test_top_three_open_enterprise_tickets(self) -> None:
        sql = self._load_sql()
        customers = self._parse_customers(sql)
        tickets = self._parse_tickets(sql)
        filtered = [
            t
            for t in tickets
            if t["status"] == "open" and customers[t["customer_id"]]["tier"] == "enterprise"
        ]
        ranked = sorted(filtered, key=lambda t: (t["priority"], t["ticket_id"]))[:3]
        names = [customers[t["customer_id"]]["name"] for t in ranked]
        subjects = [t["subject"] for t in ranked]
        assert names == ["Acme Corp", "Corex Systems", "Enterprise Co"]
        assert subjects == [
            "Login failures spike",
            "Data export timing out",
            "SSO integration broken",
        ]

    def test_filters_actually_matter(self) -> None:
        """The seed data must be arranged so that skipping EITHER the
        status filter OR the tier filter changes the top-3. Without
        this, the task-yaml's guidance is a formality. Pins the
        invariant that the fixture exercises both filters."""
        sql = self._load_sql()
        customers = self._parse_customers(sql)
        tickets = self._parse_tickets(sql)

        # Skipping the status filter (only tier filter applied)
        only_tier = sorted(
            [t for t in tickets if customers[t["customer_id"]]["tier"] == "enterprise"],
            key=lambda t: (t["priority"], t["ticket_id"]),
        )[:3]
        only_tier_ids = [t["ticket_id"] for t in only_tier]

        # Skipping the tier filter (only status filter applied)
        only_status = sorted(
            [t for t in tickets if t["status"] == "open"],
            key=lambda t: (t["priority"], t["ticket_id"]),
        )[:3]
        only_status_ids = [t["ticket_id"] for t in only_status]

        # Correct top-3
        both = sorted(
            [
                t
                for t in tickets
                if t["status"] == "open" and customers[t["customer_id"]]["tier"] == "enterprise"
            ],
            key=lambda t: (t["priority"], t["ticket_id"]),
        )[:3]
        both_ids = [t["ticket_id"] for t in both]

        assert (
            only_tier_ids != both_ids
        ), "status filter is decorative — seed needs a closed enterprise ticket that would rank"
        assert (
            only_status_ids != both_ids
        ), "tier filter is decorative — seed needs a non-enterprise open ticket that would rank"
