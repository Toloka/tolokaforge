"""Pytest configuration and shared fixtures for test suite."""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from tests.utils.secret_state import secret_manager_state_restored
from tolokaforge.core.llm.presets import set_overlay_path

pytest_plugins = ["pytester"]
"""``pytester`` is a pytest-built-in plugin that lets tests spin up an isolated
pytest session inside a temp dir. Registered at the top-level conftest so any
lane can reach it."""


@pytest.fixture(autouse=True)
def overlay_isolation():
    """Guard module-level preset-overlay state against test leakage.

    Autouse: every test in the suite gets the teardown, regardless of whether
    the test author remembered to take the fixture. The preset overlay path
    lives in module state (:data:`tolokaforge.core.llm.presets._OVERLAY_PATH`),
    so a test that installs an overlay and forgets to reset would silently
    leak into the next test in collection order — a classic discipline-based
    isolation footgun.

    Teardown cost for tests that never touch the overlay is negligible: one
    function call that sets a module global to ``None``.
    """
    yield
    # Restore the SESSION overlay (TF_PRESETS_FILE), not unconditionally None: a
    # session-wide overlay installed for the whole run - the resolve reprobe sets
    # TF_PRESETS_FILE so every probe runs under the candidate policy - must survive
    # across tests. A test that installs its OWN overlay mid-run still gets reset to
    # the session default here, so per-test isolation is preserved either way.
    set_overlay_path(os.getenv("TF_PRESETS_FILE"))


FAKE_CONTAINER_SECRETS = {
    "FAKE_JUDGE_API_KEY": "sk-fake-a$bc",
    "FAKE_SEARCH_API_KEY": "fake-search-value",
}
"""Deterministic stand-in for the host's resolved credentials. One value
carries a ``$`` because Docker Compose interpolates ``$`` in ``environment``
values — a ``$``-free payload silently disarms every assertion about the
compose-path escaping."""


@pytest.fixture
def installed_fake_secrets(request: pytest.FixtureRequest) -> Iterator[dict[str, str]]:
    """Install a fixed SecretManager as the process default, restored after.

    Tests that compare a credential payload need one that does not depend on
    the host's ``.env``: clearing the singleton instead makes the next
    ``get_default()`` lazy-init the real provider chain. Installing goes
    through ``init_default_from`` — the runner bootstrap's own path.

    Parametrise indirectly to install a different payload (``[{}]`` for a host
    that resolves no secrets). Yields the installed mapping.
    """
    from tolokaforge.secrets import SecretManager, init_default_from
    from tolokaforge.secrets import log_filter as log_filter_module

    payload: dict[str, str] = getattr(request, "param", FAKE_CONTAINER_SECRETS)
    with secret_manager_state_restored():
        init_default_from(SecretManager.from_dict(dict(payload)))
        log_filter_module._cached_manager = None
        log_filter_module._cached_values = frozenset()
        yield payload


@pytest.fixture
def env_backed_secrets(monkeypatch):
    """Pin the process ``SecretManager`` to ``os.environ`` with the shipped
    harness provider key resolvable.

    Harness mode resolves ``HarnessSpec.provider_env`` — claude-code ships
    ``${secret:OPENROUTER_API_KEY}`` — while constructing the adapter. The
    process default manager reads a ``.env`` file first, so without this a lane
    would resolve whatever credential the developer happens to have on disk and
    would fail on a machine that has none. Patching the module global (rather
    than ``init_default_from``) restores the singleton when the test ends, so no
    manager leaks into a neighbouring test's secret reads. No resolved value
    reaches a snapshot: the compose file carries names, and values live only in
    the per-trial ``.env``.

    Not autouse — a module that needs it declares
    ``pytest.mark.usefixtures("env_backed_secrets")``, so replacing the process
    secret manager stays scoped to the lanes that drive harness mode.
    """
    from tolokaforge.secrets import SecretManager
    from tolokaforge.secrets.providers import EnvProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    monkeypatch.setattr(
        "tolokaforge.secrets.manager._default_manager", SecretManager([EnvProvider()])
    )


@pytest.fixture
def write_overlay(tmp_path: Path) -> Callable[[dict], str]:
    """Return a writer that materialises an overlay dict to a temp YAML file
    and returns the path (the shape ``set_overlay_path`` consumes)."""

    def _write(data: dict, name: str = "overlay.yaml") -> str:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data))
        return str(path)

    return _write


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the parity-gate baseline refresh flag.

    The canonical parity tests under
    :mod:`tests.canonical.test_grader_parity_reference` rewrite their
    committed ``expected_grade.json`` baselines when this flag is set and
    stop before running equality assertions. Guarded so a run without the
    flag stays in assert-mode; the flag is intentionally global so mixed
    collections (``tests/canonical/`` alongside a scratch reproducer)
    still read the same option through :attr:`pytest.Config.getoption`.
    """
    parser.addoption(
        "--refresh-baselines",
        action="store_true",
        default=False,
        help=(
            "Rewrite committed grader-parity baselines from the runner_rpc "
            "leg's output and stop before asserting equality. Intended for "
            "canonical parity tests only; the resulting diff belongs in the "
            "same commit as the code change that motivated it."
        ),
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests marked with @pytest.mark.requires_api when no API keys are set."""
    api_keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
    has_api_key = any(os.environ.get(k) for k in api_keys)
    if has_api_key:
        return
    skip_marker = pytest.mark.skip(reason="No LLM API key set (requires_api)")
    for item in items:
        if "requires_api" in item.keywords:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Shared fixture imports
# ---------------------------------------------------------------------------
#
# Fixtures split into two groups:
#
#   1. Always available — generic Python/gRPC fixtures that only depend on
#      tolokaforge core and the standard library.
#   2. Docker-dependent — require the ``docker`` and ``testcontainers``
#      packages, which ship with the ``[docker]`` extra.  When that extra
#      is not installed (e.g. during unit-test-only CI runs or on a dev
#      laptop without Docker), importing these modules raises
#      ``ModuleNotFoundError`` at collection time, which blocks *every*
#      test from running — including the pure unit tests that never touch
#      Docker.
#
# We guard the docker-dependent imports so pytest can still collect and
# run the generic suite without the extra.  Tests that actually need a
# Docker fixture will fail with pytest's standard ``fixture 'X' not
# found`` message, pointing the developer at the missing extra.

from tests.utils.docker_helpers import (  # noqa: E402
    skip_if_no_docker_runner,
)
from tests.utils.fixtures import (  # noqa: E402
    canonical_project_dir,
    canonical_task_dir,
    db_client,
    db_test_client,
    mock_env_state,
    mock_grpc_context,
    runner_service,
    temp_output_dir,
    test_data_dir,
    test_task_path,
)

_DOCKER_EXTRA_FIXTURES: list[str] = []

try:  # noqa: E402
    from tests.utils.containers import (  # noqa: F401,E402
        json_db_container,
        rag_service_container,
        runner_container,
    )
    from tests.utils.networks import (  # noqa: F401,E402
        env_files_volume,
        env_network,
        rag_data_volume,
    )

    _DOCKER_EXTRA_FIXTURES = [
        "env_network",
        "env_files_volume",
        "rag_data_volume",
        "json_db_container",
        "rag_service_container",
        "runner_container",
    ]
except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
    # docker / testcontainers are shipped via the ``[docker]`` extra.  When
    # missing, we keep the rest of the suite runnable; tests that need the
    # docker fixtures will fail loudly at fixture-resolution time.
    import warnings

    warnings.warn(
        (
            "Docker test fixtures unavailable "
            f"({exc.name!r} missing). "
            "Install the '[docker]' extra to run Docker-dependent tests."
        ),
        stacklevel=1,
    )

__all__ = [
    # Utility fixtures
    "mock_env_state",
    "test_data_dir",
    "test_task_path",
    "canonical_task_dir",
    "canonical_project_dir",
    "temp_output_dir",
    # gRPC / Runner fixtures
    "mock_grpc_context",
    "db_test_client",
    "db_client",
    "runner_service",
    # Docker helper fixtures
    "skip_if_no_docker_runner",
    # Docker-extra fixtures (only available when the ``[docker]`` extra is installed)
    *_DOCKER_EXTRA_FIXTURES,
]
