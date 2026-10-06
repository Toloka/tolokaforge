"""Real keyless end-to-end run of a TB2 task through the Harbor adapter.

The single most important validation of the feature: a REAL `tolokaforge run`
over the vendored `examples/harbor/write-release-note/` pack, delegated to the
real Harbor harness with Harbor's keyless `oracle` agent (NO provider key). It
exercises the whole chain:

    build the agent image (py3.12 + harbor + docker CLI + task + tests/test.sh)
    → bring up the two-stack compose (engine + task)
    → the runner `docker exec`s `harbor run -a oracle` in the task container
    → harbor (via the mounted host socket) builds + runs its own sandbox (DooD)
    → the oracle runs solution/solve.sh → Harbor writes result.json (reward 1.0)
    → the generated tests/test.sh extracts the reward to /logs/verifier/reward.txt
    → the runner grades it `test_execution`

and asserts the parsed reward (1.0) lands in the tolokaforge grade.

Slow (nested image builds) and Docker-only: `@pytest.mark.integration` +
`requires_docker`, skipped when the Docker daemon is unreachable. The teardown
prunes any sibling containers / images Harbor or the trial stack left on the host
(the DooD orphan caveat), so the test does not leak Docker state.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACK_DIR = _REPO_ROOT / "examples" / "harbor"
_TASK_ID = "write-release-note"

# Harbor names its own sandbox images `hb__<env id>`; the adapter names each
# trial agent image `tolokaforge-harbor-<task>:<digest>`. Both are pruned.
_HARBOR_IMAGE_PREFIXES = ("hb__", "tolokaforge-harbor-")
# The adapter's per-trial compose project prefix; harbor's own inner sandbox
# containers are caught by the before/after diff below.
_TRIAL_CONTAINER_PREFIX = "tf_harbor_"

_RUN_TIMEOUT_S = 1800.0


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=120
    )


def _docker_daemon_available() -> bool:
    try:
        return _docker("info").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _container_ids() -> set[str]:
    out = _docker("ps", "-aq").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _image_ids() -> set[str]:
    out = _docker("images", "-q").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _prune(new_containers: set[str], new_images: set[str]) -> None:
    """Best-effort removal of everything this run added to the host daemon.

    Removes the containers created during the run (the trial stack and every
    sibling sandbox Harbor spun up), then the images Harbor / the adapter built.
    Named first so a container still holding an image is gone before the rmi.
    """
    for container in new_containers:
        _docker("rm", "-f", container)
    # Also sweep by name prefix, in case a container predated the snapshot race.
    ps = _docker("ps", "-aq", "--filter", f"name={_TRIAL_CONTAINER_PREFIX}").stdout
    for container in {line.strip() for line in ps.splitlines() if line.strip()}:
        _docker("rm", "-f", container)
    for image in new_images:
        _docker("rmi", "-f", image)
    # Sweep harbor / adapter images by repository name, catching any the id diff
    # missed (e.g. an image a concurrent run shared the id of).
    listed = _docker("images", "--format", "{{.ID}} {{.Repository}}").stdout
    for line in listed.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith(_HARBOR_IMAGE_PREFIXES):
            _docker("rmi", "-f", parts[0])


def _write_config(tmp_path: Path, output_dir: Path) -> Path:
    """A keyless oracle run config pointing at the vendored pack by absolute path."""
    config = {
        # Oracle runs the reference solution, not a model — a placeholder model
        # that needs no key (the engine never calls it on a delegated trial).
        "models": {
            "agent": {"provider": "openai", "name": "mockllm/model"},
            "user": {"provider": "openai", "name": "mockllm/model"},
        },
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "queue_backend": "sqlite",
            "strict_task_load": True,
            "timeouts": {"episode_s": 1800},
        },
        "evaluation": {
            "projects": [str(_PACK_DIR)],
            "tasks_glob": f"{_TASK_ID}/task.toml",
            "output_dir": str(output_dir),
            "harness_adapter": {
                "type": "harbor",
                "params": {
                    "harbor_tasks_dir": str(_PACK_DIR),
                    "task_ids": [_TASK_ID],
                    "agent": "oracle",
                    "sandbox_backend": "docker",
                    "staging_root": str(tmp_path / "staging"),
                },
            },
        },
    }
    config_path = tmp_path / "run_harbor_oracle.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return config_path


def _trial_grade(output_dir: Path) -> dict:
    """The per-trial ``grade.yaml`` the run wrote (``score`` + ``binary_pass``).

    ``tolokaforge run`` writes the run under a TIMESTAMPED SIBLING of
    ``evaluation.output_dir`` — ``<parent>/<basename>_<YYYYMMDD_HHMMSS>/`` (see
    ``resolve_run_directory``), not ``output_dir`` itself. The single-adapter
    layout inside is ``trials/<task>/<idx>/``, and the computed grade is the
    dedicated ``grade.yaml`` artifact (the reward the runner's ``test_execution``
    parsed out of Harbor's result.json) — distinct from ``trajectory.yaml``.
    """
    # Search the parent so the timestamped-sibling run dir is covered; staging
    # and the config under the same parent carry no trial bundle.
    matches = sorted(output_dir.parent.rglob("trials/**/grade.yaml"))
    assert matches, (
        f"no grade.yaml under {output_dir.parent}; run produced no graded trial. "
        f"parent contents: {sorted(p.name for p in output_dir.parent.iterdir())}"
    )
    grade = yaml.safe_load(matches[0].read_text())
    assert grade is not None, f"empty grade.yaml at {matches[0]}"
    return grade


@pytest.mark.skipif(not _docker_daemon_available(), reason="Docker daemon not available")
def test_oracle_run_grades_reference_solution(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    config_path = _write_config(tmp_path, output_dir)

    before_containers = _container_ids()
    before_images = _image_ids()
    proc: subprocess.CompletedProcess[str] | None = None
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tolokaforge.dx.cli.main",
                "run",
                "--config",
                str(config_path),
                "--image-source",
                "build",
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_S,
        )
        assert proc.returncode == 0, (
            f"harbor oracle run failed (rc={proc.returncode}):\n"
            f"stdout:\n{proc.stdout[-6000:]}\nstderr:\n{proc.stderr[-6000:]}"
        )

        # The reference solution writes the exact release note, so the Harbor
        # verifier scores 1.0 and that reward lands in the tolokaforge grade.
        grade = _trial_grade(output_dir)
        assert grade["score"] == pytest.approx(1.0), (
            f"expected oracle reward 1.0 in the grade, got {grade.get('score')}.\n"
            f"grade: {grade}\nstdout:\n{proc.stdout[-6000:]}"
        )
        assert grade["binary_pass"] is True, f"expected a passing grade; got {grade}"
    finally:
        _prune(
            _container_ids() - before_containers,
            _image_ids() - before_images,
        )
