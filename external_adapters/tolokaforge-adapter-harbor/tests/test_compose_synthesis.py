"""Unit tests for the Harbor trial compose synthesis (no Docker needed)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tolokaforge_adapter_harbor import compose_synthesis as cs

pytestmark = pytest.mark.unit

_PACK = Path(__file__).resolve().parents[3] / "examples" / "harbor" / "write-release-note"


def _materialise(tmp_path, **kwargs):
    return cs.materialise_task_environment(
        _PACK,
        "write-release-note",
        staging_root=tmp_path,
        harbor_version="0.23.0",
        base_image="python:3.12-slim-bookworm",
        **kwargs,
    )


def test_materialise_writes_build_context_and_composes(tmp_path):
    env = _materialise(tmp_path)
    sd = env.staging_dir

    assert env.compose_file.exists()
    assert env.engine_compose_file.exists()
    assert (sd / "tests" / "test.sh").exists()
    assert (sd / "task" / "task.toml").exists()

    dockerfile = (sd / "Dockerfile").read_text()
    assert 'pip install --no-cache-dir "harbor==0.23.0"' in dockerfile
    assert "COPY task /app/task" in dockerfile
    # The agent image bakes the Docker CLI + compose plugin for DooD.
    assert "docker-ce-cli docker-compose-plugin" in dockerfile

    doc = yaml.safe_load(env.compose_file.read_text())
    main = doc["services"]["main"]
    assert main["image"] == env.agent_image
    assert main["build"]["dockerfile"] == "Dockerfile"
    assert "./tests:/tests" in main["volumes"]
    assert "${TOLOKAFORGE_TRIAL_SLUG}" in main["container_name"]

    engine = yaml.safe_load(env.engine_compose_file.read_text())
    assert set(engine["services"]) == {"runner", "db-service"}


def test_agent_service_bind_mounts_the_host_docker_socket(tmp_path):
    """The DooD deviation: ``harbor run`` shells ``docker compose`` inside the
    agent container, so the agent service bind-mounts the host Docker socket."""
    env = _materialise(tmp_path)
    doc = yaml.safe_load(env.compose_file.read_text())
    volumes = doc["services"]["main"]["volumes"]
    assert f"{cs.DOCKER_SOCKET_PATH}:{cs.DOCKER_SOCKET_PATH}" in volumes


def test_test_sh_reads_reward_from_harbor_result(tmp_path):
    env = _materialise(tmp_path)
    test_sh = (env.staging_dir / "tests" / "test.sh").read_text()
    assert "/logs/verifier/reward.txt" in test_sh
    # The reward is read from the identity-mounted job dir (absolute host path),
    # not an in-container ``/logs`` path Harbor's sandbox could not bind-mount.
    assert f"{env.harbor_jobs_dir}/trial/*__*/result.json" in test_sh
    # The verifier must not import harbor.
    assert "import harbor" not in test_sh


def test_job_dir_is_identity_mounted(tmp_path):
    """Harbor's sandbox bind-mounts the job dir via the host daemon, so it must
    sit at an identical absolute path on host and in the agent container."""
    env = _materialise(tmp_path)
    doc = yaml.safe_load(env.compose_file.read_text())
    volumes = doc["services"]["main"]["volumes"]
    assert f"{env.harbor_jobs_dir}:{env.harbor_jobs_dir}" in volumes
    # Pre-created on the host so the bind mount carries host ownership.
    assert env.harbor_jobs_dir.is_dir()
    assert env.harbor_jobs_dir.name == "harbor_jobs"


def test_provider_env_keys_become_compose_env(tmp_path):
    env = _materialise(tmp_path, provider_env_keys=["ANTHROPIC_API_KEY"])
    doc = yaml.safe_load(env.compose_file.read_text())
    environment = doc["services"]["main"]["environment"]
    assert environment["ANTHROPIC_API_KEY"] == f"${{{cs.provider_input('ANTHROPIC_API_KEY')}}}"


# --- reward extraction: run the generated test.sh against fixture result.json ---


def _run_reward_extractor(tmp_path: Path, result: dict | None) -> str:
    """Render the verifier ``test.sh`` against a fixture job dir and run it.

    Writes ``result`` (when given) to the one Harbor job dir the verifier globs,
    redirects the fixed ``/logs/verifier`` output under ``tmp_path`` for this
    host-side run, runs the script, and returns the last line of the reward file.
    """
    verifier = tmp_path / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    jobs = tmp_path / "harbor_jobs"
    if result is not None:
        job = jobs / "trial" / "write-release-note__abc1234"
        job.mkdir(parents=True, exist_ok=True)
        (job / "result.json").write_text(json.dumps(result))

    script = cs._render_test_sh(cs._result_glob(jobs))
    script = script.replace(f"{cs.CONTAINER_LOGS_DIR}/verifier", str(verifier))
    script_path = tmp_path / "test.sh"
    script_path.write_text(script)
    subprocess.run(["bash", str(script_path)], check=True)
    return (verifier / "reward.txt").read_text().strip().splitlines()[-1]


def test_reward_extraction_single_step(tmp_path):
    result = {"exception_info": None, "verifier_result": {"rewards": {"reward": 1.0}}}
    assert float(_run_reward_extractor(tmp_path, result)) == 1.0


def test_reward_extraction_single_step_fail(tmp_path):
    result = {"exception_info": None, "verifier_result": {"rewards": {"reward": 0.0}}}
    assert float(_run_reward_extractor(tmp_path, result)) == 0.0


def test_reward_extraction_multi_step_mean(tmp_path):
    result = {
        "exception_info": None,
        "step_results": [
            {"verifier_result": {"rewards": {"reward": 1.0}}},
            {"verifier_result": {"rewards": {"reward": 0.0}}},
        ],
    }
    assert float(_run_reward_extractor(tmp_path, result)) == 0.5


def test_reward_extraction_exception_scores_zero(tmp_path):
    result = {
        "exception_info": {"type": "RuntimeError", "message": "boom"},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    assert float(_run_reward_extractor(tmp_path, result)) == 0.0


def test_reward_extraction_missing_result_scores_zero(tmp_path):
    assert float(_run_reward_extractor(tmp_path, None)) == 0.0
