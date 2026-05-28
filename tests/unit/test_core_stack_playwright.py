"""Verify core_stack opts the Runner image into Playwright when requested.

The orchestrator scans task configs for the browser tool and passes
``enable_playwright=True`` to ``core_stack()``. The runner ServiceDefinition
must surface this as the ``INSTALL_PLAYWRIGHT=true`` build arg so the
Dockerfile installs Playwright + Chromium. When disabled (the default),
no build arg leaks through and the image stays slim.
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
