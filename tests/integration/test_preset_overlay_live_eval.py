"""End-to-end live eval: a real ``tolokaforge run`` driven by a preset
overlay file, against a public tool-use task. Verifies the per-trial
``task.yaml`` output records the overlay's preset name in the resolved
fingerprint — proof that an operator can register a model registration
delta without an engine release and see it land in the run artifacts.

**Gated.** Requires Docker (for the runner container stack) and a real LLM
provider key (currently OPENROUTER_API_KEY). The top-level
``tests/conftest.py`` auto-skips ``requires_api``-marked tests when no key
is set; the Docker check below skips when the daemon isn't available.

**Cost.** One trial of one tool-use task (max 8 turns) on
``anthropic/claude-sonnet-4-6`` via OpenRouter, with the standard
``cache_policy: anthropic_ephemeral`` baseline. Single run costs cents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.requires_docker,
    pytest.mark.llm,
    pytest.mark.slow,
]


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# Single-task pack — the second public tool-use task is omitted by glob so
# the test stays at one trial.
_PUBLIC_TASK_PACK = "examples/native/tool_use/dataset"
_PUBLIC_TASK_GLOB = "**/tool_use_public_example_01/task.yaml"
_OVERLAY_PRESET_NAME = "live_eval_overlay_marker_preset"


def _pick_provider() -> tuple[str, str] | None:
    """Pick a (provider, model) pair whose credential is available in env.

    Anthropic-direct first (lowest provider surface area for this Claude
    workload), then OpenRouter. Returns ``None`` if neither key is set —
    the test that calls this is gated by a skipif on the same condition.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("anthropic", "claude-sonnet-4-6")
    if os.environ.get("OPENROUTER_API_KEY"):
        return ("openrouter", "anthropic/claude-sonnet-4-6")
    return None


def _provider_credential_available() -> bool:
    return _pick_provider() is not None


@pytest.fixture
def chosen_provider() -> tuple[str, str]:
    """Return the (provider, model) the test will drive."""
    picked = _pick_provider()
    assert picked is not None, "no provider credential available"
    return picked


@pytest.fixture
def overlay_file(tmp_path: Path, chosen_provider: tuple[str, str]) -> Path:
    """Write an overlay that shadows the bundled match for the agent model.

    The overlay uses a recognisably distinct preset name so the assertion
    below can confirm the *overlay* won (and not the bundled ``anthropic``
    preset). Cache policy stays ``anthropic_ephemeral`` to keep the eval
    behaviour identical to a non-overlay run — we are testing the seam,
    not perturbing the workload.
    """
    _, model_name = chosen_provider
    overlay = {
        "presets": {
            _OVERLAY_PRESET_NAME: {
                "match": [f"{model_name}*", f"*{model_name}*"],
                "content_policy": "anthropic",
                "reasoning_codec": "anthropic",
                "cache_policy": "anthropic_ephemeral",
            }
        }
    }
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    return path


@pytest.fixture
def run_config_file(tmp_path: Path, chosen_provider: tuple[str, str]) -> Path:
    """Write a run config pointing at the public single-task pack."""
    provider, model_name = chosen_provider
    output_dir = tmp_path / "results"
    cfg = {
        "models": {
            "agent": {
                "provider": provider,
                "name": model_name,
                "temperature": 0.0,
                "max_tokens": 4096,
            },
            "user": {
                "provider": provider,
                "name": model_name,
                "temperature": 0.2,
            },
        },
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "max_turns": 8,
            "queue_backend": "sqlite",
        },
        "evaluation": {
            "task_packs": [_PUBLIC_TASK_PACK],
            "tasks_glob": _PUBLIC_TASK_GLOB,
            "output_dir": str(output_dir),
            "cache_images": True,
        },
    }
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(
    not _provider_credential_available(),
    reason=(
        "Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY set — at least one "
        "is required to reach a Claude model for the eval."
    ),
)
def test_live_eval_records_overlay_preset_in_trial_output(
    overlay_file: Path, run_config_file: Path, tmp_path: Path
) -> None:
    """Drive ``tolokaforge run`` with an overlay and assert the per-trial
    ``task.yaml`` records the overlay's preset name in the resolved block.

    The overlay's only behavioural job here is to *rename* the preset (same
    policy slots as the bundled ``anthropic`` preset). That keeps the eval
    workload identical while proving the overlay's metadata reaches the
    output artifacts.
    """
    env = os.environ.copy()
    # Pass the API key through; conftest gating ensures we only get here
    # when at least one provider key is set.
    proc = subprocess.run(
        [
            "uv",
            "run",
            "tolokaforge",
            "run",
            "--config",
            str(run_config_file),
            "--presets-file",
            str(overlay_file),
        ],
        cwd=str(Path.cwd()),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,  # 15 minutes — Docker build can be slow on a cold cache
        check=False,
    )
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )

    # Locate the per-trial task.yaml. The orchestrator suffixes ``output_dir``
    # with ``_<timestamp>`` and writes per-trial output under
    # ``<output_dir>_<ts>/trials/<task_id>/<trial_idx>/task.yaml``.
    # Search the *parent* of the configured output_dir so the timestamp suffix
    # is in scope.
    configured = Path(yaml.safe_load(run_config_file.read_text())["evaluation"]["output_dir"])
    search_root = configured.parent
    trial_yamls = list(search_root.glob("**/trials/*/*/task.yaml"))
    assert trial_yamls, (
        f"no per-trial task.yaml found under {search_root}.\n"
        f"search root contents: {list(search_root.glob('**/*'))[:50]}\n"
        f"--- stdout tail ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr tail ---\n{proc.stderr[-2000:]}"
    )

    # Read the first (and only, given repeats=1 + 1-task glob) trial.
    trial_yaml = yaml.safe_load(trial_yamls[0].read_text())
    agent_block = trial_yaml.get("model_config", {}).get("agent", {})
    resolved = agent_block.get("resolved", {})
    assert resolved, (
        f"trial {trial_yamls[0]} missing model_config.agent.resolved; " f"got: {agent_block}"
    )
    assert resolved.get("effective_preset") == _OVERLAY_PRESET_NAME, (
        f"expected overlay preset {_OVERLAY_PRESET_NAME!r} in resolved block, " f"got {resolved!r}"
    )
    # Policy slots set by the overlay must be reflected too — proves the
    # overlay's content reached capability resolution, not just the name tag.
    assert resolved.get("content_policy") == "anthropic"
    assert resolved.get("reasoning_codec") == "anthropic"
    assert resolved.get("cache_policy") == "anthropic_ephemeral"
