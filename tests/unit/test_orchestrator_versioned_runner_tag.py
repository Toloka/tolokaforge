"""Unit tests for :meth:`Orchestrator._ensure_versioned_runner_image_tag`.

The hook applies ``docker tag <content-hash> tolokaforge-runner:local``
after the shared stack starts, giving per-trial task compose files a
stable name to reference (the raw content-hash tag changes on every
source edit and would break any external reference).

Uses a fake ``ServiceStack`` / ``Image`` pair so the tests don't touch
the Docker daemon or the real image-build path — the hook's contract
under test is "look up the runner image, apply the alias, log", not
"actually rebuild an image."
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _make_orchestrator() -> Orchestrator:
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )
    return Orchestrator(config)


class _FakeImage:
    """Records ``add_alias_tag`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.should_raise: Exception | None = None

    def add_alias_tag(self, alias_repository: str, alias_tag: str) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.calls.append((alias_repository, alias_tag))


class _FakeServiceStack:
    """Minimal stand-in for :class:`ServiceStack` — only ``get_image`` is used."""

    def __init__(self, runner_image: _FakeImage | None) -> None:
        self._runner = runner_image

    def get_image(self, service_name: str) -> _FakeImage | None:
        if service_name == "runner":
            return self._runner
        return None


class TestVersionedRunnerImageTag:
    """The hook aliases the freshly-built runner image with the
    pinned-version tag task compose files reference.
    """

    def test_applies_alias_with_stable_local_tag(self) -> None:
        orch = _make_orchestrator()
        image = _FakeImage()
        stack = _FakeServiceStack(runner_image=image)

        orch._ensure_versioned_runner_image_tag(stack)

        assert image.calls == [("tolokaforge-runner", "local")]

    def test_no_runner_service_is_debug_logged_and_returns(self) -> None:
        """When the stack has no runner service (test-only or future
        variants), the hook logs and returns without raising."""
        orch = _make_orchestrator()
        orch.logger = MagicMock()
        stack = _FakeServiceStack(runner_image=None)

        orch._ensure_versioned_runner_image_tag(stack)

        # No image → no alias attempted. Hook logs at DEBUG.
        orch.logger.debug.assert_called_once()

    def test_alias_failure_is_warning_logged_not_raised(self) -> None:
        """The hook is best-effort: if the Docker daemon rejects the
        alias, the shared-stack path still works with the content-hash
        tag. Only per-trial task compose files referencing
        ``tolokaforge-runner:local`` would then fail — which is a
        user-visible error at that point, not at run-start."""
        orch = _make_orchestrator()
        orch.logger = MagicMock()
        image = _FakeImage()
        image.should_raise = RuntimeError("docker daemon rejected the tag")
        stack = _FakeServiceStack(runner_image=image)

        orch._ensure_versioned_runner_image_tag(stack)  # no raise

        orch.logger.warning.assert_called_once()
        assert "Failed to apply runner-image alias tag" in (orch.logger.warning.call_args.args[0])
