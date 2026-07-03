"""Unit tests for :meth:`Orchestrator._ensure_engine_image_local_aliases`.

The hook applies ``docker tag <content-hash> <repo>:local`` after the
shared stack starts, giving per-trial task compose files stable names
to reference (the raw content-hash tags change on every source edit
and would break any external reference).

Uses a fake ``ServiceStack`` / ``Image`` pair so the tests don't touch
the Docker daemon or the real image-build path — the hook's contract
under test is "look up each engine image, apply the alias, log", not
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
from tolokaforge.docker.image import ImageError

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

    def __init__(self, images: dict[str, _FakeImage] | None = None) -> None:
        self._images = images or {}

    def get_image(self, service_name: str) -> _FakeImage | None:
        return self._images.get(service_name)


class TestEngineImageLocalAliases:
    """The hook aliases every freshly-built engine image with the stable
    ``:local`` tag task compose files reference.
    """

    def test_applies_local_alias_to_runner_and_db_service(self) -> None:
        orch = _make_orchestrator()
        runner_img = _FakeImage()
        db_img = _FakeImage()
        stack = _FakeServiceStack({"runner": runner_img, "db-service": db_img})

        orch._ensure_engine_image_local_aliases(stack)

        assert runner_img.calls == [("tolokaforge-runner", "local")]
        assert db_img.calls == [("tolokaforge-db-service", "local")]

    def test_missing_service_is_debug_logged_and_loop_continues(self) -> None:
        """When the stack has only one of the engine services (test-only
        or future stack shapes), the hook logs the missing one at DEBUG
        and still aliases the present one. Loop-not-abort behaviour."""
        orch = _make_orchestrator()
        orch.logger = MagicMock()
        runner_img = _FakeImage()
        stack = _FakeServiceStack({"runner": runner_img})  # no db-service

        orch._ensure_engine_image_local_aliases(stack)

        assert runner_img.calls == [("tolokaforge-runner", "local")]
        orch.logger.debug.assert_called_once()

    def test_alias_failure_is_warning_logged_and_loop_continues(self) -> None:
        """The hook is best-effort: if the Docker daemon rejects one
        alias, the shared-stack path still works with the content-hash
        tag. Failures on one service don't abort the loop — the other
        service still gets its alias if the daemon accepts it."""
        orch = _make_orchestrator()
        orch.logger = MagicMock()
        runner_img = _FakeImage()
        runner_img.should_raise = ImageError(
            "tag", "tolokaforge-runner:hashhash", "daemon rejected the tag"
        )
        db_img = _FakeImage()
        stack = _FakeServiceStack({"runner": runner_img, "db-service": db_img})

        orch._ensure_engine_image_local_aliases(stack)  # no raise

        # runner alias failed — logged as warning.
        orch.logger.warning.assert_called_once()
        assert "Failed to apply engine-image alias tag" in (orch.logger.warning.call_args.args[0])
        # db-service alias still succeeded — loop continued.
        assert db_img.calls == [("tolokaforge-db-service", "local")]

    def test_non_image_error_bubbles_up_as_bug(self) -> None:
        """The best-effort catch narrows to :class:`ImageError` — a
        generic exception (``AttributeError`` / ``TypeError``) signals
        a genuine coding bug (wrong-typed return from ``get_image``,
        broken ``add_alias_tag`` signature) and should not be silently
        logged. Rule 1 fail-fast."""
        orch = _make_orchestrator()
        runner_img = _FakeImage()
        runner_img.should_raise = AttributeError("get_image returned a wrong-typed object")
        stack = _FakeServiceStack({"runner": runner_img})

        with pytest.raises(AttributeError, match="wrong-typed object"):
            orch._ensure_engine_image_local_aliases(stack)
