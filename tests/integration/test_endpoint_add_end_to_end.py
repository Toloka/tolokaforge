"""End-to-end proof for the ``multi_service_endpoint_add`` example pack.

The pack's stack is ``agent (runner) -> source (named volume) -> testrunner
(runs the real suite) -> app-db:16 (orders + customers)``, plus the engine
``db-service``. The ``source`` volume is mounted into both the ``runner`` (as
``/work``, the agent's workspace) and the ``testrunner`` (as ``/workspace``,
the ``filesystem_dir`` reset target), so the tree the agent edits is the tree
the tests run against. ``testrunner`` is ``isolation: reset`` bound to the
``pristine_source`` directory seed, so the per-trial backend restores the
pristine FastAPI source into that shared volume before every trial. Two tests
lock two distinct behaviours:

* **Test A — deterministic recipe + bridge proof (no LLM, $0).** Provisions the
  pack's stack via a real :class:`PerTrialRuntimeBackend` (with the pack's seed
  registry, so the ``filesystem_dir`` recipe actually restores the source tree),
  then witnesses two things while the stack is up: the pristine seed reached the
  runner's ``/work`` (``app.py`` matches the seed and ``tests/test_summary.py``
  exists) — **proving the reset recipe fired across the shared-volume bridge** —
  and the test-execution wiring is real — ``POST /run-tests`` on the unedited
  source yields ``FAIL`` (the target test fails on pristine code), and a re-run
  after the reference endpoint is written over the shared volume yields ``PASS``.
  No LLM key, no agent loop; only the source edit is scripted.
* **Test B — runnable + trace inspection (one Haiku run, ~$0.50-1.50).** Runs
  the pack's ``run_config.yaml`` through ``tolokaforge run`` as a subprocess and
  asserts *infrastructure + traces, not agent correctness*: the run exits 0, the
  per-trial backend was selected (the reset seam ran), ``repeats: 2`` produced
  two trial dirs, and the agent drove the auto-dev loop — a ``write_file`` edit
  and an ``http_request`` to ``.../run-tests``. Deliberately *out of scope*:
  ``binary_pass``, judge success — those are agent-correctness / model-flaky
  concerns.

**Gated.** Both require a real Docker daemon (the per-trial runtime brings up the
compose stack). Test B additionally needs a real LLM provider key;
``requires_api`` auto-skips without one (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from testcontainers.compose import DockerCompose

from tests.canonical._factories import make_task_description
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.models import ModelConfig, SeedRef
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.project_loader import load_project_config, resolve
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = "examples/native/multi_service_endpoint_add"
_PROJECT_YAML = _REPO_ROOT / _PACK / "project.yaml"
_RUN_CONFIG = f"{_PACK}/run_config.yaml"
_TASK_ID = "endpoint_add"
_SEED_DIR = _REPO_ROOT / _PACK / "assets" / "source"

# The reference endpoint appended to the pristine source to turn the target
# test green: the order joined with its customer in a single query, reusing the
# app.state pool the seeded app.py already opens. Test A scripts this edit (no
# LLM) to prove the marker tracks the real suite result.
_SUMMARY_ROUTE = '''

@app.get("/orders/{order_id}/summary")
async def order_summary(order_id: int) -> dict[str, Any]:
    row = await app.state.pool.fetchrow(
        """
        SELECT o.order_id, o.product, o.status, o.amount::float8 AS amount,
               c.customer_id, c.name, c.email, c.tier
        FROM orders o JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.order_id = $1
        """,
        order_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "order_id": row["order_id"],
        "product": row["product"],
        "status": row["status"],
        "amount": row["amount"],
        "customer": {
            "customer_id": row["customer_id"],
            "name": row["name"],
            "email": row["email"],
            "tier": row["tier"],
        },
    }
'''


def _pack_manifest_and_seeds() -> tuple[EnvironmentManifest, dict[str, SeedRef]]:
    """Build the pack's resolved :class:`EnvironmentManifest` and its seed
    registry the way the orchestrator does — ``load_project_config`` verifies
    the ``pristine_source`` directory-seed digest and anchors paths absolute;
    ``resolve`` materialises the manifest (``testrunner`` ``isolation: reset``
    bound to ``pristine_source``)."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None, "pack default_environment did not resolve to a manifest"
    seeds = dict(project.assets.seeds)
    return manifest, seeds


# ---------------------------------------------------------------------------
# Test A — deterministic recipe + bridge proof (no LLM, $0)
# ---------------------------------------------------------------------------


def _compose_exec(
    compose: DockerCompose, service: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose exec -T <service> <args...>`` against the started
    stack, from the compose project's context directory."""
    cmd = [*compose.docker_compose_command(), "exec", "-T", service, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=compose.context)


def _post_run_tests(compose: DockerCompose) -> dict[str, Any]:
    """Invoke the testrunner's ``POST /run-tests`` from inside its own
    container and return the parsed JSON payload
    (``{"passed", "returncode", "output"}``)."""
    py = (
        "import urllib.request;"
        "req=urllib.request.Request('http://127.0.0.1:8000/run-tests',method='POST');"
        "print(urllib.request.urlopen(req,timeout=120).read().decode())"
    )
    result = _compose_exec(compose, "testrunner", "python", "-c", py)
    return json.loads(result.stdout)


def _read_marker(compose: DockerCompose) -> str:
    """Read the ``test_result.txt`` marker from the runner's ``/work`` — the
    testrunner writes it into ``/workspace`` (the shared volume), so reading it
    from the runner side proves the marker synced back across the bridge."""
    return _compose_exec(compose, "runner", "cat", "/work/test_result.txt").stdout.strip()


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial runtime needs it)",
)
class TestEndpointAddRecipeAndBridge:
    """Provisioning the pack's stack fires the ``filesystem_dir`` recipe into
    the runner's ``/work`` over the shared volume, and the testrunner's
    ``POST /run-tests`` tracks the real suite result: ``FAIL`` on pristine
    source, ``PASS`` once the reference endpoint is written. The stack, the
    reset recipe, and the test execution are all real — only the source edit is
    scripted (no LLM)."""

    def _trial_spec(self, manifest: EnvironmentManifest) -> TrialSpec:
        return TrialSpec(
            trial_id=f"{_TASK_ID}:0",
            run_id="endpoint-add-recipe-bridge",
            task=make_task_description(
                task_id=_TASK_ID,
                name="endpoint-add",
                category="multi_service",
                description="endpoint-add recipe + bridge integration test",
                environment_manifest=manifest,
            ),
            agent_model_config=ModelConfig(name="claude-haiku-4-5", provider="anthropic"),
            env_endpoints=EnvEndpoints(
                db_url="http://placeholder:5432",
                runner_url="http://placeholder:50051",
            ),
        )

    def test_recipe_seeds_shared_volume_and_test_result_tracks_suite(self, tmp_path: Path) -> None:
        manifest, seeds = _pack_manifest_and_seeds()
        backend = PerTrialRuntimeBackend(seeds=seeds)
        handle = backend.provision(self._trial_spec(manifest))
        compose = handle.compose  # type: ignore[attr-defined]
        try:
            # (a) The pristine seed reached the runner's /work — proves the
            # filesystem_dir recipe fired across the shared-volume bridge.
            seeded_app = _compose_exec(compose, "runner", "cat", "/work/app.py").stdout
            assert seeded_app == (_SEED_DIR / "app.py").read_text(), (
                "runner:/work/app.py does not match the pristine seed — the "
                "filesystem_dir recipe did not seed the shared volume."
            )
            _compose_exec(compose, "runner", "test", "-f", "/work/tests/test_summary.py")

            # (b) The test-execution wiring is real: the target test fails on
            # the pristine source and the marker records FAIL.
            pristine = _post_run_tests(compose)
            assert pristine["passed"] is False, (
                f"POST /run-tests passed on pristine source — the target test "
                f"should fail before the endpoint exists.\npayload: {pristine}"
            )
            assert _read_marker(compose) == "FAIL", "marker is not FAIL on pristine source"

            # Write the reference endpoint over the shared volume (docker cp so
            # the multi-line file lands verbatim) and re-run: the marker flips.
            reference = tmp_path / "app_reference.py"
            reference.write_text((_SEED_DIR / "app.py").read_text() + _SUMMARY_ROUTE)
            subprocess.run(
                [
                    *compose.docker_compose_command(),
                    "cp",
                    str(reference),
                    "testrunner:/workspace/app.py",
                ],
                check=True,
                capture_output=True,
                cwd=compose.context,
            )
            fixed = _post_run_tests(compose)
            assert fixed["passed"] is True, (
                f"POST /run-tests still failed after the reference endpoint was "
                f"written — the suite does not track the edited source.\npayload: {fixed}"
            )
            assert _read_marker(compose) == "PASS", "marker did not flip to PASS after the fix"
        finally:
            backend.teardown(handle)


# ---------------------------------------------------------------------------
# Test B — runnable + trace inspection (one Haiku run, ~$0.50-1.50)
# ---------------------------------------------------------------------------


def _output_basename() -> str:
    """The configured ``output_dir`` basename the orchestrator suffixes with a
    run timestamp (``<basename>_<YYYYmmdd_HHMMSS>``)."""
    cfg = yaml.safe_load((_REPO_ROOT / _RUN_CONFIG).read_text())
    return Path(cfg["evaluation"]["output_dir"]).name


def _agent_tool_calls(trajectory: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every tool call recorded in the trajectory's messages."""
    for msg in trajectory.get("messages") or []:
        yield from msg.get("tool_calls") or []


def _called_write_file(trajectory: dict[str, Any]) -> bool:
    """True iff the agent invoked the ``write_file`` tool — the code edit."""
    return any(call.get("name") == "write_file" for call in _agent_tool_calls(trajectory))


def _called_run_tests(trajectory: dict[str, Any]) -> bool:
    """True iff the agent invoked ``http_request`` against the testrunner's
    ``/run-tests`` endpoint — proves the agent-in-container -> testrunner
    network path and the run-the-suite step of the auto-dev loop."""
    for call in _agent_tool_calls(trajectory):
        if call.get("name") != "http_request":
            continue
        if "run-tests" in str((call.get("arguments") or {}).get("url", "")):
            return True
    return False


def _trial_dirs(run_dir: Path) -> list[Path]:
    """The per-trial output dirs under ``trials/<task>/`` (one per repeat)."""
    task_dir = run_dir / "trials" / _TASK_ID
    return sorted(p for p in task_dir.iterdir() if p.is_dir()) if task_dir.is_dir() else []


def _assert_infrastructure(run_dir: Path) -> None:
    """Assert the run's artifacts prove ``repeats: 2`` produced two trials and
    the agent drove the auto-dev loop (a ``write_file`` edit + an
    ``http_request`` to ``/run-tests``) in at least one of them."""
    trial_dirs = _trial_dirs(run_dir)
    assert len(trial_dirs) == 2, (
        f"expected 2 trial dirs (repeats: 2); got {[p.name for p in trial_dirs]}.\n"
        f"run dir contents: {sorted(p.name for p in run_dir.rglob('*'))[:50]}"
    )

    wrote_file = False
    ran_tests = False
    for trial_dir in trial_dirs:
        trajectory_path = trial_dir / "trajectory.yaml"
        assert trajectory_path.exists(), f"missing trajectory.yaml at {trajectory_path}"
        trajectory = yaml.safe_load(trajectory_path.read_text())
        wrote_file = wrote_file or _called_write_file(trajectory)
        ran_tests = ran_tests or _called_run_tests(trajectory)

    assert wrote_file, (
        "no write_file tool call across either trial — the agent never edited "
        "the source. The auto-dev edit path did not run."
    )
    assert ran_tests, (
        "no http_request to .../run-tests across either trial — the agent never "
        "triggered the suite. The agent -> testrunner path did not run."
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
def test_endpoint_add_infrastructure_and_auto_dev_loop() -> None:
    """The full ``tolokaforge run`` exits 0 and its captured traces prove the
    per-trial backend ran the reset seam, ``repeats: 2`` produced two trials,
    and the agent drove the auto-dev loop (wrote source, ran the suite over
    ``http_request`` to the testrunner). Agent correctness (``binary_pass``,
    judge score) is deliberately out of scope."""
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
