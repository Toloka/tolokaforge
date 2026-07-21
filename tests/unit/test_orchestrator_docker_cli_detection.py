"""Orchestrator must bake the docker CLI into the runner image only for the
terminal-bench adapter, which shells out to docker inside the runner.

Every other run builds the slim default image without the CLI (#539). The
detection mirrors the Playwright pre-stack scan but keys off the configured
adapter type rather than task tool names.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.orchestrator import _run_needs_docker_cli
from tolokaforge.runner.models import AdapterType

pytestmark = pytest.mark.unit


def test_terminal_bench_string_triggers_docker_cli():
    assert _run_needs_docker_cli("terminal_bench") is True


def test_terminal_bench_enum_triggers_docker_cli():
    assert _run_needs_docker_cli(AdapterType.TERMINAL_BENCH) is True


def test_native_adapter_returns_false():
    assert _run_needs_docker_cli("native") is False


def test_other_adapter_returns_false():
    assert _run_needs_docker_cli(AdapterType.TAU) is False


def test_no_adapter_returns_false():
    assert _run_needs_docker_cli(None) is False
