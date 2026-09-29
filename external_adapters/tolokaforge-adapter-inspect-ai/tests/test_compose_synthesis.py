"""Unit tests for the inspect trial compose synthesis (no Docker needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tolokaforge_adapter_inspect_ai import compose_synthesis as cs

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _materialise(tmp_path, **kwargs):
    return cs.materialise_task_environment(
        _FIXTURES,
        "poc_smoke",
        staging_root=tmp_path,
        inspect_version="0.3.271",
        base_image="python:3.12-slim-bookworm",
        **kwargs,
    )


def test_materialise_writes_build_context_and_composes(tmp_path):
    env = _materialise(tmp_path)
    sd = env.staging_dir

    assert env.compose_file.exists()
    assert env.engine_compose_file.exists()
    assert (sd / "tests" / "test.sh").exists()
    assert (sd / "task_pack" / "poc_task.py").exists()

    dockerfile = (sd / "Dockerfile").read_text()
    assert "inspect-ai==0.3.271" in dockerfile
    assert "COPY task_pack /app/task_pack" in dockerfile

    doc = yaml.safe_load(env.compose_file.read_text())
    main = doc["services"]["main"]
    assert main["image"] == env.agent_image
    assert main["build"]["dockerfile"] == "Dockerfile"
    assert "./tests:/tests" in main["volumes"]
    assert "${TOLOKAFORGE_TRIAL_SLUG}" in main["container_name"]

    engine = yaml.safe_load(env.engine_compose_file.read_text())
    assert set(engine["services"]) == {"runner", "db-service"}


def test_test_sh_reads_reward_from_eval_log(tmp_path):
    env = _materialise(tmp_path)
    test_sh = (env.staging_dir / "tests" / "test.sh").read_text()
    assert "/logs/verifier/reward.txt" in test_sh
    assert "read_eval_log" in test_sh
    assert "/logs/inspect/*.eval" in test_sh


def test_extra_pip_packages_land_in_dockerfile(tmp_path):
    env = _materialise(tmp_path, extra_pip_packages=["openai", "anthropic"])
    dockerfile = (env.staging_dir / "Dockerfile").read_text()
    assert '"openai"' in dockerfile
    assert '"anthropic"' in dockerfile


def test_provider_env_keys_become_compose_env(tmp_path):
    env = _materialise(tmp_path, provider_env_keys=["OPENAI_API_KEY"])
    doc = yaml.safe_load(env.compose_file.read_text())
    environment = doc["services"]["main"]["environment"]
    assert environment["OPENAI_API_KEY"] == f"${{{cs.provider_input('OPENAI_API_KEY')}}}"
