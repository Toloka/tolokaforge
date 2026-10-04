"""Delegated-execution wiring for the Harbor adapter (no Docker).

Covers the four seams a delegated Harbor trial rides on that are resolvable
without a daemon: the synthesised compose resolving the Harbor dialect vars,
the ``test_execution`` grading payload carrying the task's verifier timeout,
provider-env resolution (``${secret:...}`` expansion plus refusal of a value
that cannot be written to the per-trial ``.env``), and the delegated harness
command the conductor dispatches in place of the engine loop. Materialisation
writes a staging tree but builds no image and runs no container.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tolokaforge_adapter_harbor.adapter import HarborAdapter, _resolve_provider_env

pytestmark = pytest.mark.unit

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "harbor"
_FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "harbor_tasks"
_EXAMPLE_TASK = "write-release-note"
_DIALECT_TASK = "dialect-compose"

# A cheap model so the delegated command names a real route without implying a
# run should pick it; no request is made in these tests.
_HARNESS = "claude-code"
_MODEL = "openrouter/anthropic/claude-3.5-haiku"


@pytest.fixture
def env_secrets(monkeypatch: pytest.MonkeyPatch):
    """Pin the process ``SecretManager`` to ``os.environ`` with test secrets.

    Mirrors the engine's ``env_backed_secrets`` fixture so ``${secret:NAME}``
    resolves against env vars this test controls, never a developer's ``.env``.
    """
    from tolokaforge.secrets import SecretManager
    from tolokaforge.secrets.providers import EnvProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    monkeypatch.setenv("HARBOR_FAKE_KEY", "sk-harbor-fake")
    monkeypatch.setenv("HARBOR_BAD_KEY", "bad$value")
    monkeypatch.setattr(
        "tolokaforge.secrets.manager._default_manager", SecretManager([EnvProvider()])
    )


def test_synthesised_compose_resolves_the_harbor_dialect(tmp_path: Path) -> None:
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_FIXTURE_DIR),
            "task_ids": [_DIALECT_TASK],
            "staging_root": str(tmp_path),
            "prebuild_images": False,
        }
    )
    env = adapter._environment(_DIALECT_TASK)
    text = env.compose_file.read_text()
    # Every Harbor-dialect placeholder the fixture authored is substituted.
    for token in ("${CONTEXT_DIR}", "${MAIN_IMAGE_NAME}", "${ENV_AGENT_LOGS_PATH}"):
        assert token not in text, f"{token} left unresolved in the synthesised compose"

    doc = yaml.safe_load(text)
    main = doc["services"]["main"]
    assert main["image"] == env.agent_image  # MAIN_IMAGE_NAME resolved
    assert main["build"]["context"] == "./environment"  # CONTEXT_DIR resolved
    env_entries = dict(e.split("=", 1) for e in main["environment"])
    # The ENV_*_LOGS_PATH dialect vars resolve to the harness log container paths.
    assert env_entries["AGENT_LOGS"] == "/logs/agent"
    assert env_entries["VERIFIER_LOGS"] == "/logs/verifier"


def test_grading_payload_is_test_execution_with_the_verifier_timeout(tmp_path: Path) -> None:
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_EXAMPLES_DIR),
            "task_ids": [_EXAMPLE_TASK],
            "staging_root": str(tmp_path),
            "prebuild_images": False,
        }
    )
    task = adapter.to_task_description(_EXAMPLE_TASK)
    assert task.adapter_type == "harbor"
    assert task.grading.grading_method == "test_execution"
    # task.toml declares [verifier] timeout_sec = 60.0, which rides the payload.
    assert task.grading.grading_method_config["timeout_s"] == 60.0


def test_provider_env_resolves_a_secret_reference(env_secrets: None) -> None:
    resolved = _resolve_provider_env(
        {}, {"OPENROUTER_API_KEY": "${secret:HARBOR_FAKE_KEY}"}, "claude-code"
    )
    assert resolved == {"OPENROUTER_API_KEY": "sk-harbor-fake"}


def test_provider_env_refuses_an_unrepresentable_value(env_secrets: None) -> None:
    # The resolved secret contains a `$`, which would start a compose
    # interpolation in the per-trial `.env`; the adapter refuses it by key.
    with pytest.raises(ValueError, match="newline or a `\\$`"):
        _resolve_provider_env({}, {"OPENROUTER_API_KEY": "${secret:HARBOR_BAD_KEY}"}, "claude-code")


def test_agent_harness_command_is_built_for_a_delegated_run(
    tmp_path: Path, env_secrets: None
) -> None:
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_EXAMPLES_DIR),
            "task_ids": [_EXAMPLE_TASK],
            "staging_root": str(tmp_path),
            "prebuild_images": False,
            "agent_harness": _HARNESS,
            "agent_model": _MODEL,
        }
    )
    metadata = adapter.to_task_description(_EXAMPLE_TASK).metadata
    assert metadata["agent_harness"] == _HARNESS
    assert metadata["agent_harness_model"] == _MODEL
    command = metadata["agent_harness_command"]
    assert isinstance(command, str) and command.strip()
    # The forwarded credential is wired through provider-env, never baked into
    # the command string.
    assert "sk-openrouter-test" not in command
