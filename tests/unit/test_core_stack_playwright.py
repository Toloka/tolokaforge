"""Verify core_stack opts the Runner image into optional heavy toolchains.

The orchestrator scans task configs for tools that need a heavy runtime and
sets the matching ``core_stack()`` flag: ``enable_playwright=True`` for the
browser tool, ``enable_docker_cli=True`` for terminal-bench (which shells out
to ``docker``). Each flag must surface as its own build arg on the runner
ServiceDefinition (``INSTALL_PLAYWRIGHT=true`` / ``INSTALL_DOCKER_CLI=true``)
so the multi-stage Dockerfile pulls in the extra toolchain. When disabled
(the default), no build arg leaks through and the image stays slim.
"""

from __future__ import annotations

import pytest

from tolokaforge.docker.stacks.core import core_stack

pytestmark = pytest.mark.unit


def _runner_def(stack):
    runner = stack.services.get("runner")
    assert runner is not None, "runner ServiceDefinition not found in stack"
    return runner


def test_default_runner_has_no_playwright_build_arg():
    stack = core_stack()
    runner = _runner_def(stack)
    assert "INSTALL_PLAYWRIGHT" not in runner.build_args


def test_enable_playwright_sets_build_arg():
    stack = core_stack(enable_playwright=True)
    runner = _runner_def(stack)
    assert runner.build_args.get("INSTALL_PLAYWRIGHT") == "true"


def test_enable_playwright_false_omits_build_arg():
    stack = core_stack(enable_playwright=False)
    runner = _runner_def(stack)
    assert "INSTALL_PLAYWRIGHT" not in runner.build_args


def test_default_runner_has_no_docker_cli_build_arg():
    stack = core_stack()
    runner = _runner_def(stack)
    assert "INSTALL_DOCKER_CLI" not in runner.build_args


def test_enable_docker_cli_sets_build_arg():
    stack = core_stack(enable_docker_cli=True)
    runner = _runner_def(stack)
    assert runner.build_args.get("INSTALL_DOCKER_CLI") == "true"


def test_enable_docker_cli_false_omits_build_arg():
    stack = core_stack(enable_docker_cli=False)
    runner = _runner_def(stack)
    assert "INSTALL_DOCKER_CLI" not in runner.build_args
