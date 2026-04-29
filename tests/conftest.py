"""Pytest configuration and shared fixtures for test suite."""

import os

import pytest


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
