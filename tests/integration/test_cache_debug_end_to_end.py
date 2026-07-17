"""End-to-end proof for the ``multi_service_cache_debug`` example pack.

The pack's stack is ``agent -> orders-api + cache-admin (FastAPI) ->
redis:7-alpine (cache, isolation: reset) + postgres:16 (app-db, source of
truth)``, plus the engine ``runner`` + ``db-service``. ``redis`` is
``isolation: reset`` bound to the ``cache_poisoned`` RDB seed, so the per-trial
backend restores the poisoned cache before every trial. Two tests lock two
distinct behaviours:

* **Test A — deterministic red-grade capture (no LLM, $0).** Provisions the
  pack's stack via a real :class:`PerTrialRuntimeBackend` (with the pack's seed
  registry, so the ``redis_dump`` recipe actually restores ``dump.rdb``), then
  drives a completed-but-red trial through :class:`ProvisioningTrialExecutor`
  with an :class:`InMemoryConductor`. Asserts the captured ``services/redis.log``
  carries an RDB-load signature — **proving the ``redis_dump`` recipe fired** —
  and that the FastAPI + postgres services also produced captured logs —
  **proving multi-service capture on a completed-but-red grade**. No LLM
  key, no agent loop; only the trial outcome is deterministic.
* **Test B — runnable + two-layer inspection (one Haiku run, ~$0.50-1.50).**
  Runs the pack's ``run_config.yaml`` through ``tolokaforge run`` as a
  subprocess and asserts *infrastructure + traces, not agent correctness*: the
  run exits 0, the per-trial backend was selected (the reset seam ran), the
  agent reached both the app layer (``orders-api:8000``) and the cache layer
  (``cache-admin:8000``) with 2xx GETs, wrote a ``submissions/`` note, and the
  ``capture_logs_on_success`` ``redis.log`` again carries the RDB-load
  signature. Deliberately *out of scope*: ``binary_pass``, ``state_checks``,
  judge success — those are agent-correctness / model-flaky concerns.

**Gated.** Both require a real Docker daemon (the per-trial runtime brings up
the compose stack). Test B additionally needs a real LLM provider key;
``requires_api`` auto-skips without one (see ``tests/conftest.py``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_task_description
from tests.integration.docker.test_per_trial_log_capture_integration import (
    _completed_red_factory,
    _write_metrics,
)
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig, SeedRef
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.project_loader import load_project_config, resolve
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = "examples/native/multi_service_cache_debug"
_PROJECT_YAML = _REPO_ROOT / _PACK / "project.yaml"
_RUN_CONFIG = f"{_PACK}/run_config.yaml"
_TASK_ID = "cache_debug"

# Redis logs one of these when it loads an RDB snapshot at startup — the
# recipe's restart reloads the seeded ``dump.rdb``, so the presence of any of
# them in ``redis.log`` proves the ``redis_dump`` recipe fired.
_RDB_LOAD_SIGNATURES = ("Loading RDB", "Done loading RDB", "DB loaded from disk")

# The FastAPI + postgres services whose captured logs prove multi-service
# capture (redis is asserted separately for its RDB-load signature).
_APP_SERVICES = ("orders-api", "cache-admin", "app-db")

# The two service hostnames the agent must reach with a 2xx GET — the app layer
# and the cache layer (two-layer inspection).
_APP_LAYER_HOST = "orders-api:8000"
_CACHE_LAYER_HOST = "cache-admin:8000"


def _pack_manifest_and_seeds() -> tuple[EnvironmentManifest, dict[str, SeedRef]]:
    """Build the pack's resolved :class:`EnvironmentManifest` and its seed
    registry the way the orchestrator does — ``load_project_config`` verifies
    the seed digest and anchors paths absolute; ``resolve`` materialises the
    manifest (``redis`` ``isolation: reset`` bound to ``cache_poisoned``)."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None, "pack default_environment did not resolve to a manifest"
    seeds = dict(project.assets.seeds)
    return manifest, seeds


# ---------------------------------------------------------------------------
# Test A — deterministic red-grade capture (no LLM, $0)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial runtime needs it)",
)
class TestCacheDebugRedGradeCapture:
    """A completed-but-red trial on the pack's stack fires the ``redis_dump``
    recipe (RDB-load evidence in ``redis.log``) and captures every declared
    service's logs before teardown. The stack, the reset recipe, the
    compose-logs subprocess, and the capture are all real — only the trial
    outcome is deterministic."""

    def _trial_spec(self, manifest: EnvironmentManifest) -> TrialSpec:
        return TrialSpec(
            trial_id=f"{_TASK_ID}:0",
            run_id="cache-debug-red-capture",
            task=make_task_description(
                task_id=_TASK_ID,
                name="cache-debug",
                category="multi_service",
                description="cache-debug red-grade capture integration test",
                environment_manifest=manifest,
            ),
            agent_model_config=ModelConfig(name="claude-haiku-4-5", provider="anthropic"),
            env_endpoints=EnvEndpoints(
                db_url="http://placeholder:5432",
                runner_url="http://placeholder:50051",
            ),
        )

    def test_completed_but_red_grade_captures_recipe_firing_and_all_services(
        self, tmp_path: Path
    ) -> None:
        manifest, seeds = _pack_manifest_and_seeds()
        log_capture = LogCaptureConfig(output_root=tmp_path, tail=200, on_success=False)
        backend = PerTrialRuntimeBackend(seeds=seeds, log_capture=log_capture)
        executor = ProvisioningTrialExecutor(
            runtime_backend=backend,
            conductor=InMemoryConductor(trajectory_factory=_completed_red_factory),
            logger=StructuredLogger("test-cache-debug-red-capture"),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
        )
        metrics_path = _write_metrics(tmp_path, _TASK_ID, 0)

        # execute() owns provision (incl. the redis_dump recipe) + teardown.
        result = executor.execute(self._trial_spec(manifest), make_task_config(task_id=_TASK_ID))
        assert result.trajectory.grade is not None
        assert result.trajectory.grade.binary_pass is False

        services_dir = tmp_path / "trials" / _TASK_ID / "0" / "services"
        assert services_dir.is_dir(), f"no services dir at {services_dir}"

        redis_log = services_dir / "redis.log"
        assert redis_log.is_file(), "missing redis.log — reset recipe / capture did not run"
        redis_text = redis_log.read_text()
        assert redis_log.stat().st_size > 0, "empty redis.log"
        assert any(sig in redis_text for sig in _RDB_LOAD_SIGNATURES), (
            "redis.log carries no RDB-load signature — the redis_dump recipe did not "
            f"reload dump.rdb.\nexpected one of {_RDB_LOAD_SIGNATURES}\nredis.log:\n{redis_text}"
        )

        for service in _APP_SERVICES:
            log_file = services_dir / f"{service}.log"
            assert log_file.is_file(), f"missing {service}.log — multi-service log capture gap"
            assert log_file.stat().st_size > 0, f"empty {service}.log"

        metrics = yaml.safe_load(metrics_path.read_text())
        captured = metrics["captured_service_logs"]
        for service in ("redis", *_APP_SERVICES):
            assert service in captured, f"{service} absent from metrics.captured_service_logs"
            file_bytes = (services_dir / f"{service}.log").stat().st_size
            assert captured[service] == file_bytes, f"metrics byte count for {service} mismatch"
        assert metrics["cost_usd"] == 0.5, "pre-existing metrics key lost in the amendment"


# ---------------------------------------------------------------------------
# Test B — runnable + two-layer inspection (one Haiku run, ~$0.50-1.50)
# ---------------------------------------------------------------------------


def _output_basename() -> str:
    """The configured ``output_dir`` basename the orchestrator suffixes with a
    run timestamp (``<basename>_<YYYYmmdd_HHMMSS>``)."""
    cfg = yaml.safe_load((_REPO_ROOT / _RUN_CONFIG).read_text())
    return Path(cfg["evaluation"]["output_dir"]).name


def _status_by_tool_call_id(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Map each ``tool_call_id`` to the HTTP status parsed from its tool-result
    message content (``http_request`` output begins ``Status: <code>``). Lets a
    2xx be tied back to the specific ``http_request`` that produced it."""
    statuses: dict[str, int] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id:
            continue
        match = re.search(r"Status:\s*(\d{3})", str(msg.get("content") or ""))
        if match:
            statuses[tool_call_id] = int(match.group(1))
    return statuses


def _hosts_reached_with_2xx_get(trajectory: dict[str, Any]) -> set[str]:
    """The set of ``{host}`` fragments the agent hit with a 2xx GET
    ``http_request`` — each call correlated to its result status by
    ``tool_call_id``. Proves the agent-in-container -> FastAPI network path per
    layer."""
    messages = trajectory.get("messages") or []
    statuses = _status_by_tool_call_id(messages)
    reached: set[str] = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if call.get("name") != "http_request":
                continue
            args = call.get("arguments") or {}
            if str(args.get("method", "GET")).upper() != "GET":
                continue
            status = statuses.get(call.get("id"))
            if status is None or not (200 <= status < 300):
                continue
            url = str(args.get("url", ""))
            reached.update(host for host in (_APP_LAYER_HOST, _CACHE_LAYER_HOST) if host in url)
    return reached


def _wrote_submissions_note(trajectory: dict[str, Any]) -> bool:
    """True iff the agent called ``write_file`` with a ``path`` under
    ``submissions/`` — the written root-cause deliverable."""
    for msg in trajectory.get("messages") or []:
        for call in msg.get("tool_calls") or []:
            if call.get("name") != "write_file":
                continue
            path = str((call.get("arguments") or {}).get("path", ""))
            if "submissions/" in path:
                return True
    return False


def _assert_infrastructure(run_dir: Path) -> None:
    """Assert the run's artifacts prove the pack's infrastructure, two-layer
    inspection, the written deliverable, and the redis_dump restore. Split out
    so the assertions can be dry-run against a preserved run dir."""
    trial_dir = run_dir / "trials" / _TASK_ID / "0"
    trajectory_path = trial_dir / "trajectory.yaml"
    metrics_path = trial_dir / "metrics.yaml"
    for path in (trajectory_path, metrics_path):
        assert path.exists(), (
            f"missing {path.name} at {path}.\n"
            f"run dir contents: {sorted(p.name for p in run_dir.rglob('*'))[:50]}"
        )

    trajectory = yaml.safe_load(trajectory_path.read_text())
    reached = _hosts_reached_with_2xx_get(trajectory)
    missing = [host for host in (_APP_LAYER_HOST, _CACHE_LAYER_HOST) if host not in reached]
    assert not missing, (
        f"agent did not reach {missing} with a 2xx GET — two-layer inspection "
        f"(app + cache) did not work.\nreached: {sorted(reached)}\n"
        f"messages: {trajectory.get('messages')}"
    )

    assert _wrote_submissions_note(trajectory), (
        "no write_file to a submissions/ path in the trajectory — the agent did not "
        f"write a root-cause note.\nmessages: {trajectory.get('messages')}"
    )

    metrics = yaml.safe_load(metrics_path.read_text())
    assert metrics["tool_calls"] > 0, f"metrics report no tool calls.\nmetrics: {metrics}"
    assert metrics["cost_usd"] > 0, f"metrics report zero cost.\nmetrics: {metrics}"

    redis_log = trial_dir / "services" / "redis.log"
    assert redis_log.is_file(), "missing services/redis.log — capture_logs_on_success did not fire"
    redis_text = redis_log.read_text()
    assert any(sig in redis_text for sig in _RDB_LOAD_SIGNATURES), (
        "redis.log carries no RDB-load signature — the redis_dump recipe did not reload "
        f"dump.rdb on the runnable path.\nexpected one of {_RDB_LOAD_SIGNATURES}\n"
        f"redis.log:\n{redis_text}"
    )


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.requires_api
@pytest.mark.llm
@pytest.mark.slow
@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial runtime needs it)",
)
def test_cache_debug_infrastructure_and_two_layer_inspection() -> None:
    """The full ``tolokaforge run`` exits 0 and its captured traces prove the
    per-trial backend ran the reset seam, the agent inspected both the app and
    cache layers (2xx GETs), wrote a submissions/ note, and the redis_dump
    recipe reloaded the poisoned RDB (``redis.log`` RDB-load signature)."""
    basename = _output_basename()
    results_root = _REPO_ROOT / "results"
    before = set(results_root.glob(f"{basename}_*")) if results_root.exists() else set()

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", _RUN_CONFIG],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )

    combined = proc.stdout + proc.stderr
    assert "runtime.backend.selected" in combined and "PerTrialRuntimeBackend" in combined, (
        "run did not select PerTrialRuntimeBackend — the reset seam did not run.\n"
        f"stdout:\n{proc.stdout[-4000:]}"
    )

    after = set(results_root.glob(f"{basename}_*"))
    created = after - before
    assert len(created) == 1, (
        f"expected exactly one new run dir under {results_root} matching "
        f"{basename}_*; got {sorted(created)}"
    )
    run_dir = created.pop()

    _assert_infrastructure(run_dir)

    # Only reached once every assertion passed — a failing run leaves the run
    # dir (with all traces + per-service logs) on disk for post-mortem.
    shutil.rmtree(run_dir, ignore_errors=True)
