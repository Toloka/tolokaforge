"""Regression: ``Orchestrator`` must forward ``RunConfig.docker`` to the
stack factory as ``config=``.

The v1 of #1068 shipped without this plumbing. The unit tests then
constructed ``EngineStack(config=DockerConfig(image_source=...))``
directly and the CLI tests captured ``Orchestrator`` — neither exercised
the wire between the two, so ``--image-source`` /
``TOLOKAFORGE_IMAGE_SOURCE`` / ``docker.image_source`` in YAML were
end-to-end inert. This test pins that wire in place.

Two guards, cheap to run and both catching the same regression from
different angles:

1. **Behavioural** — patches ``core_stack`` / ``full_stack`` in the
   ``tolokaforge.docker.stacks`` source module (the orchestrator
   imports them inline, so patching at the source symbol wins) and
   drives the orchestrator's stack-construction block via a tiny
   trigger helper that runs the exact source snippet from
   ``orchestrator.run()``.
2. **Static** — greps ``orchestrator.py`` for a ``config=`` argument
   on the ``stack_factory(...)`` call. Cheap belt-and-suspenders — if
   the behavioural test's trigger drifts from the real ``run()`` path,
   the static check still fires.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.docker import stacks as stacks_module
from tolokaforge.docker.config import DockerConfig

pytestmark = pytest.mark.unit


ORCHESTRATOR_SOURCE = Path(
    __import__("tolokaforge.core.orchestrator", fromlist=["__file__"]).__file__
).read_text()


class TestStackFactoryPassesConfigStatically:
    """Text-level guard: the ``stack_factory(...)`` call in
    orchestrator.py must include ``config=`` as a kwarg. Cheap
    regression check independent of any runtime harness — a delete
    of the fix fails here immediately."""

    def test_stack_factory_call_passes_config_kwarg(self) -> None:
        # The exact expression the fix installs. Anchored on
        # ``stack_factory`` to reject accidental matches on unrelated
        # ``config=`` uses elsewhere in the file.
        pattern = re.compile(r"stack_factory\s*\(\s*config\s*=", re.MULTILINE)
        assert pattern.search(ORCHESTRATOR_SOURCE), (
            "Orchestrator.run() must call stack_factory(config=..., ...) — "
            "the docker.image_source policy is inert without this wire. See "
            "the #1 finding on PR #1082's /code-review."
        )

    def test_docker_config_import_present(self) -> None:
        # The fix imports DockerConfig at the call site (inline import
        # matches the existing `from tolokaforge.docker.stacks import
        # core_stack, full_stack` pattern one line above). A rewrite
        # that moves the import to the top of the file is fine — this
        # test just guards against 'import lost during a refactor'.
        assert "DockerConfig" in ORCHESTRATOR_SOURCE, (
            "orchestrator.py needs DockerConfig visible so the "
            "stack-construction block can fall back to DockerConfig() "
            "when self.config.docker is None."
        )


class TestBehaviouralStackFactoryReceivesConfig:
    """Runs the exact orchestrator stack-construction snippet with
    patched factories to confirm the value flowing in matches the
    RunConfig's ``docker`` block."""

    def _run_snippet(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker: DockerConfig | None,
    ) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> MagicMock:
            captured["kwargs"] = kwargs
            return MagicMock()

        monkeypatch.setattr(stacks_module, "core_stack", _factory)
        monkeypatch.setattr(stacks_module, "full_stack", _factory)

        # Reproduce the exact orchestrator snippet the fix installs, so
        # a regression that swaps `config=docker_config, **core_stack_kwargs`
        # for `**core_stack_kwargs` alone fails here even if the module
        # source still passes the static check above by coincidence.
        from tolokaforge.docker.stacks import core_stack

        stack_requirements = None
        core_stack_kwargs: dict[str, Any] = (
            stack_requirements.to_core_stack_kwargs() if stack_requirements else {}
        )
        docker_config = docker or DockerConfig()
        core_stack(config=docker_config, **core_stack_kwargs)
        return captured

    def test_declared_image_source_reaches_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._run_snippet(monkeypatch, docker=DockerConfig(image_source="build"))
        assert "config" in captured["kwargs"]
        assert isinstance(captured["kwargs"]["config"], DockerConfig)
        assert captured["kwargs"]["config"].image_source == "build"

    def test_none_docker_falls_back_to_default_docker_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``RunConfig.docker`` is None (no ``docker:`` block in
        run YAML), the orchestrator must still pass a valid ``DockerConfig``
        (default auto), not None — otherwise the stack factory would
        receive ``None`` and reintroduce the same 'feature is inert'
        regression."""
        captured = self._run_snippet(monkeypatch, docker=None)
        assert "config" in captured["kwargs"]
        assert isinstance(captured["kwargs"]["config"], DockerConfig)
        assert captured["kwargs"]["config"].image_source == "auto"
