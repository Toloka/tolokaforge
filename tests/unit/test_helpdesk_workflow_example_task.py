"""Static-shape + adversarial-seed tests for the ``helpdesk_01`` task pack.

The task pack under ``examples/native/multi_service_helpdesk_workflow/`` is the
flagship cross-service-reasoning workload: the agent reconciles four FastAPI
business services plus an in-container postgres-FTS policy corpus over one
postgres substrate, derives the one policy-valid resolution (``reschedule``),
and grading checks that value directly against the substrate. Real docker
execution lives in the integration suite; here we pin, without docker, the
pack's static shape, the grading wiring, and — the load-bearing check — the
seed invariant that makes the grader's ``reschedule`` assertion non-decorative.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import EnvironmentPatch, GradingConfig

pytestmark = pytest.mark.unit


_PACK_DIR = (
    Path(__file__).parent.parent.parent / "examples" / "native" / "multi_service_helpdesk_workflow"
)
_SHARED = _PACK_DIR / "shared"
_TASK_DIR = _PACK_DIR / "dataset" / "tasks" / "helpdesk_01"
_COMPOSE = _SHARED / "environment.compose.yaml"
_INIT_SQL = _SHARED / "app-db" / "init.sql"

_APP_SERVICES = {
    "delivery-tracker",
    "product-catalog",
    "client-locations",
    "crm",
    "policy-search",
}
_ALL_SERVICES = _APP_SERVICES | {"runner", "db-service", "app-db"}
_APP_HOSTS = {f"{svc}:8000" for svc in _APP_SERVICES}
_FLOATING_TAGS = {"latest", "edge", "stable", "main", "master"}


def _load_compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def _load_grading() -> GradingConfig:
    return GradingConfig.model_validate(yaml.safe_load((_TASK_DIR / "grading.yaml").read_text()))


def _matched_calls(node: object) -> set[tuple[str, str]]:
    """Every ``(url, method)`` a matcher compares against, at any nesting depth."""
    if isinstance(node, dict):
        args = node.get("args") or {}
        found = (
            {(args["url"]["equals"], args["method"]["equals"])}
            if "url" in args and "method" in args
            else set()
        )
        return found.union(*(_matched_calls(value) for value in node.values()), set())
    if isinstance(node, list):
        return set().union(*(_matched_calls(item) for item in node), set())
    return set()


class TestPackLayoutOnDisk:
    def test_all_pack_files_exist(self) -> None:
        expected = [
            _PACK_DIR / "project.yaml",
            _PACK_DIR / "run_configs" / "dev.yaml",
            _PACK_DIR / "README.md",
            _COMPOSE,
            _INIT_SQL,
            _TASK_DIR / "task.yaml",
            _TASK_DIR / "grading.yaml",
        ]
        expected += [_SHARED / svc / "main.py" for svc in _APP_SERVICES]
        missing = [str(p) for p in expected if not p.is_file()]
        assert not missing, f"missing pack files: {missing}"


class TestComposeShape:
    def test_declares_exactly_eight_named_services(self) -> None:
        compose = _load_compose()
        assert set(compose["services"].keys()) == _ALL_SERVICES

    def test_images_pinned(self) -> None:
        compose = _load_compose()
        expected_image = {
            "runner": "tolokaforge-runner:local",
            "db-service": "tolokaforge-db-service:local",
            "app-db": "postgres:16",
            **dict.fromkeys(_APP_SERVICES, "tolokaforge-runner:local"),
        }
        for svc, image in expected_image.items():
            assert compose["services"][svc]["image"] == image
            tag = image.split(":", 1)[1]
            assert tag not in _FLOATING_TAGS

    def test_app_services_bind_mount_own_main_read_only(self) -> None:
        compose = _load_compose()
        for svc in _APP_SERVICES:
            mounts = compose["services"][svc]["volumes"]
            has_ro_mount = any(
                m.startswith(f"./{svc}/main.py:") and m.endswith(":ro") for m in mounts
            )
            assert has_ro_mount, f"{svc}: no read-only bind mount of its own main.py in {mounts!r}"

    def test_app_services_depend_on_healthy_db(self) -> None:
        compose = _load_compose()
        for svc in _APP_SERVICES:
            depends = compose["services"][svc]["depends_on"]
            assert depends["app-db"]["condition"] == "service_healthy"

    def test_runner_depends_on_all_apps_and_db_service(self) -> None:
        compose = _load_compose()
        depends = set(compose["services"]["runner"]["depends_on"].keys())
        assert depends == _APP_SERVICES | {"db-service"}

    def test_compose_under_line_budget(self) -> None:
        line_count = len(_COMPOSE.read_text().splitlines())
        assert line_count < 150, f"compose file is {line_count} lines (budget < 150)"


class TestTaskManifest:
    def test_loads_via_task_loader(self) -> None:
        task_config, task_root = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.task_id == "helpdesk_01"
        assert task_root == _TASK_DIR

    def test_declares_environment_manifest(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert isinstance(task_config.environment_manifest, EnvironmentPatch)

    def test_app_db_ephemeral_others_shared(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        services = task_config.environment_manifest.services
        assert services is not None
        assert services["app-db"].isolation == "ephemeral"
        assert all(
            spec.isolation == "shared" for name, spec in services.items() if name != "app-db"
        )

    def test_allowed_hosts_list_all_five_app_services(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        allowed = set(task_config.tools.agent["http_request"]["allowed_hosts"])
        assert allowed == _APP_HOSTS


class TestInitSql:
    def _sql(self) -> str:
        return _INIT_SQL.read_text()

    def test_creates_read_only_grader_role(self) -> None:
        sql = self._sql()
        assert "CREATE ROLE grader" in sql
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO grader" in sql
        for verb in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE", "GRANT ALL"):
            assert verb not in sql, f"grader role must be read-only; found {verb!r}"

    def test_crm_cases_ships_empty(self) -> None:
        sql = self._sql()
        assert "CREATE TABLE crm_cases" in sql
        assert "INSERT INTO crm_cases" not in sql


def _parse_sites(sql: str) -> dict[str, dict[str, bool]]:
    """Extract the ``sites`` INSERT rows into a
    ``{customer_id: {has_temp_storage, has_specialist}}`` dict. Regex-based —
    we pin the seed shape without asking postgres to run it."""
    m = re.search(r"INSERT INTO sites\s*\([^)]+\)\s*VALUES\s*(.+?);", sql, re.DOTALL)
    assert m is not None, "sites INSERT not found in init.sql"
    row_re = re.compile(
        r"\(\s*'([^']+)'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,"
        r"\s*(TRUE|FALSE)\s*,\s*(TRUE|FALSE)\s*\)"
    )
    return {
        cid: {"has_temp_storage": temp == "TRUE", "has_specialist": spec == "TRUE"}
        for cid, temp, spec in row_re.findall(m.group(1))
    }


def _defensible_path(has_temp_storage: bool, has_specialist: bool) -> str:
    """The resolution path the after-hours policy permits for a temperature-
    sensitive shipment arriving after the staffed window, given a site's
    capabilities: on-site cold storage → hold, else a certified specialist →
    handoff, else the shipment must be rescheduled."""
    if has_temp_storage:
        return "temp_controlled_hold"
    if has_specialist:
        return "specialist_handoff"
    return "reschedule"


class TestAdversarialSeedInvariant:
    """The load-bearing check (mirrors
    ``test_multi_service_postgres_example_task.py::test_filters_actually_matter``):
    the seed must be arranged so both alternative paths are excluded *only* by
    the data, making the grader's ``reschedule`` assertion non-decorative."""

    def _grading_target(self) -> str:
        grading = _load_grading()
        crm_probe = next(
            p for p in grading.state_checks.db_probes if p["name"] == "crm_case_policy_correct"
        )
        expect = next(e for e in crm_probe["expect"] if e["path"] == "$.rows[0].resolution_path")
        return expect["equals"]

    def test_northwind_capabilities_force_reschedule(self) -> None:
        sites = _parse_sites(_INIT_SQL.read_text())
        northwind = sites["NORTHWIND"]
        assert northwind["has_temp_storage"] is False
        assert northwind["has_specialist"] is False
        assert self._grading_target() == "reschedule"
        assert _defensible_path(False, False) == "reschedule"

    def test_either_capability_would_change_the_path(self) -> None:
        assert _defensible_path(True, False) != "reschedule"
        assert _defensible_path(False, True) != "reschedule"


class TestGradingWiring:
    def test_parses_to_grading_config(self) -> None:
        grading = _load_grading()
        assert grading.state_checks is not None
        assert grading.trace_checks is not None
        assert grading.llm_judge is not None

    def test_db_probes_present_with_exact_shape(self) -> None:
        grading = _load_grading()
        probes = {p["name"]: p for p in grading.state_checks.db_probes}
        assert set(probes) == {"crm_case_policy_correct", "delivery_annotated"}
        for probe in probes.values():
            assert probe["dsn"] == "postgresql://grader:grader_pw@app-db:5432/helpdesk"
        deliv_expect = probes["delivery_annotated"]["expect"][0]
        assert deliv_expect["path"] == "$.rows[0].resolution_path"
        assert deliv_expect["equals"] == "reschedule"

    def test_trace_constraints_address_the_packs_own_endpoints(self) -> None:
        grading = _load_grading()
        matched = _matched_calls(grading.trace_checks.model_dump())
        assert matched == {
            ("http://policy-search:8000/search", "POST"),
            ("http://crm:8000/cases", "POST"),
            ("http://delivery-tracker:8000/deliveries/4021", "PATCH"),
        }
        unknown = {urlparse(url).netloc for url, _ in matched} - _APP_HOSTS
        assert not unknown, f"constraints address {unknown}, which this pack has no service for"

    def test_weights_sum_and_threshold(self) -> None:
        grading = _load_grading()
        weights = grading.combine.weights
        assert weights == {"state_checks": 0.6, "trace_checks": 0.25, "llm_judge": 0.15}
        assert abs(sum(weights.values()) - 1.0) < 1e-9
        assert grading.combine.pass_threshold == 0.6
