"""The standalone Python example driver drives a real graded trial.

The behaviour lock for ``deploy/standalone/examples/drive_one_trial.py``: bring
the local-mode composed stack up under the compose project the driver targets by
default, run the driver as a cold user would (``python drive_one_trial.py``), and
assert it drives one real trial to a non-null grade. The driver's own
``TrialResult.model_validate`` on the wire result is the typed lock — an exit 0
means a well-formed graded ``TrialResult`` came back over the ``run-trial`` exec
wire. Paid (one real LLM trial), so ``requires_api``-gated off the keyless lane;
the keyless shape guard is ``tests/canonical/test_standalone_example_drivers.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest

from tests.integration.deploy.conftest import (
    REPO_ROOT,
    StackHandle,
    build_and_tag_local,
    compose,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.slow,
]

_EXAMPLES_DIR = REPO_ROOT / "deploy" / "standalone" / "examples"
_PYTHON_DRIVER = _EXAMPLES_DIR / "drive_one_trial.py"

# The driver invokes ``docker compose -f <recipe>`` with no ``-p``, so it targets
# Compose's default project — the basename of the recipe's directory. The stack
# must come up under that same name for the driver's ``cp`` / ``exec`` to find it.
_DRIVER_PROJECT = "standalone"

# rag-service's cold-start model download can exceed two minutes; floor the
# compose wait above the repo's 180s rag health timeout so ``up --wait`` does not
# trip on a cold cache.
_COMPOSE_WAIT_TIMEOUT_S = 300


def _pick_provider() -> tuple[str, str] | None:
    """(provider, model) whose credential is available via ``SecretManager``."""
    from tolokaforge.secrets import get_default

    secrets = get_default()
    if secrets.get_secret("ANTHROPIC_API_KEY"):
        return ("anthropic", "claude-sonnet-4-6")
    if secrets.get_secret("OPENROUTER_API_KEY"):
        return ("openrouter", "anthropic/claude-sonnet-4-6")
    return None


@pytest.fixture(scope="module")
def local_stack(docker_daemon: None) -> Iterator[StackHandle]:
    """Bring the standalone recipe up from locally-built ``:local`` images.

    Under the driver's default compose project so the driver drives this stack.
    """
    build_and_tag_local()
    up = compose(
        _DRIVER_PROJECT,
        ["up", "-d", "--wait", "--wait-timeout", str(_COMPOSE_WAIT_TIMEOUT_S)],
        "local",
    )
    try:
        assert (
            up.returncode == 0
        ), f"`compose up --wait` failed (rc={up.returncode}):\n{up.stdout}\n{up.stderr}"
        yield StackHandle(mode="local", project=_DRIVER_PROJECT, tag="local")
    finally:
        compose(_DRIVER_PROJECT, ["down", "-v"], "local")


@pytest.mark.requires_api
@pytest.mark.llm
def test_python_driver_drives_graded_trial(local_stack: StackHandle) -> None:
    """``drive_one_trial.py`` drives one real trial to a non-null grade."""
    provider = _pick_provider()
    if provider is None:
        pytest.skip("needs an ANTHROPIC or OPENROUTER key")
    provider_name, model = provider

    driver_env = {
        **os.environ,
        "TOLOKAFORGE_IMAGE_TAG": "local",
        "TOLOKAFORGE_EXAMPLE_PROVIDER": provider_name,
        "TOLOKAFORGE_EXAMPLE_MODEL": model,
    }
    proc = subprocess.run(
        [sys.executable, str(_PYTHON_DRIVER)],
        cwd=_EXAMPLES_DIR,
        capture_output=True,
        text=True,
        env=driver_env,
    )
    assert proc.returncode == 0, (
        f"driver exited non-zero (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    grade = proc.stdout.strip()
    assert grade and grade != "None", f"driver printed no grade (stdout: {proc.stdout!r})"
