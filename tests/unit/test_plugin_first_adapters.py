"""Plugin-first adapter system: the *runner* must not branch on adapter identity.

These pin the capability/declarative seams that replace the old runner-side
``adapter_type == AdapterType.TERMINAL_BENCH`` branches (tool lifecycle + grading
method), the open ``adapter_type`` string, and the host-side registry guard — so a
new adapter can be added as an entry-point package with zero engine edits.
"""

import pytest

from tolokaforge.adapters import (
    available_adapters,
    ensure_registered_adapter,
    register_adapter,
)
from tolokaforge.runner.models import GradingConfig, TaskDescription
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    ToolLifecycleContext,
    ToolWrapper,
)

pytestmark = pytest.mark.unit


class TestOpenAdapterType:
    def test_unknown_adapter_type_round_trips(self):
        """A non-built-in adapter name serializes and deserializes unchanged.

        (Before opening the enum this raised a ValidationError, blocking any
        entry-point adapter from round-tripping a TaskDescription.)
        """
        td = TaskDescription(
            task_id="t1",
            name="t1",
            category="general",
            description="d",
            adapter_type="some_third_party_adapter",
            system_prompt="sp",
        )
        assert td.adapter_type == "some_third_party_adapter"
        reloaded = TaskDescription.model_validate_json(td.model_dump_json())
        assert reloaded.adapter_type == "some_third_party_adapter"


class TestDeclarativeGradingMethod:
    def test_grading_method_defaults_none(self):
        assert GradingConfig().grading_method is None

    def test_grading_method_round_trips(self):
        gc = GradingConfig(grading_method="test_execution")
        assert GradingConfig.model_validate_json(gc.model_dump_json()).grading_method == (
            "test_execution"
        )


class TestToolLifecycleCapability:
    def test_base_wrapper_has_no_lifecycle(self):
        assert ToolWrapper.has_lifecycle is False

    def test_compose_wrapper_declares_lifecycle(self):
        assert DockerComposeExecToolWrapper.has_lifecycle is True

    def test_base_start_stop_are_noops(self):
        """A tool without a lifecycle is a safe no-op under the generic runner loop."""

        class _Dummy(ToolWrapper):
            async def execute(self, arguments):  # pragma: no cover - not called
                return ""

        from tolokaforge.runner.models import ToolSchema

        tool = _Dummy(ToolSchema(name="x", description="", parameters={}))
        assert tool.has_lifecycle is False
        # Must not raise — the runner calls these on every tool generically.
        tool.start(ToolLifecycleContext(trial_id="t:0"))
        tool.stop()


class TestHostSideAdapterTypeGuard:
    """adapter_type is an open string; the host validates it against the registry
    (the runner stays permissive). See ``ensure_registered_adapter``."""

    def test_accepts_registered_adapter(self):
        # 'native' is always registered; should not raise.
        assert "native" in available_adapters()
        ensure_registered_adapter("native")

    def test_rejects_unknown_adapter(self):
        with pytest.raises(ValueError, match="Unknown adapter type"):
            ensure_registered_adapter("definitely_not_a_real_adapter")

    def test_accepts_a_freshly_registered_plugin(self):
        """A newly registered (entry-point-style) adapter passes with no engine edit."""
        import tolokaforge.adapters as adapters_mod

        register_adapter("temp_plugin_adapter", object)
        try:
            ensure_registered_adapter("temp_plugin_adapter")
        finally:
            adapters_mod._ADAPTERS.pop("temp_plugin_adapter", None)


class TestRunnerStaysAdapterAgnostic:
    """Regression guard: the runner must select behaviour from declarative
    capabilities (``has_lifecycle``, ``grading_method``), never from the adapter's
    identity. Fails if an ``adapter_type ==`` / ``== AdapterType`` / adapter
    ``isinstance`` branch is reintroduced into runner behavioural code.
    """

    def test_runner_behavioural_modules_have_no_adapter_identity_branches(self):
        import re
        from pathlib import Path

        import tolokaforge.runner.service as service_mod
        import tolokaforge.runner.tool_factory as tool_factory_mod

        # Patterns that indicate branching on a specific adapter's identity.
        identity_patterns = [
            re.compile(r"adapter_type\s*=="),
            re.compile(r"==\s*AdapterType\b"),
            re.compile(r"isinstance\([^)]*Adapter\b"),
        ]
        offenders: list[str] = []
        for mod in (service_mod, tool_factory_mod):
            src = Path(mod.__file__).read_text()
            for i, line in enumerate(src.splitlines(), start=1):
                if any(p.search(line) for p in identity_patterns):
                    offenders.append(f"{mod.__name__}:{i}: {line.strip()}")

        assert not offenders, (
            "Runner must stay adapter-agnostic (drive behaviour from capabilities, "
            "not adapter identity). Found identity branch(es):\n" + "\n".join(offenders)
        )


class TestGradingMethodErrorHandling:
    """Adapter-author-facing error paths: clear, actionable, non-silent."""

    def test_invalid_grading_method_string_is_rejected_by_the_model(self):
        """Typos in ``grading_method`` are caught at validation, not silently ignored."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            GradingConfig(grading_method="test-execution")  # hyphen — should be underscore

    def test_test_execution_without_exec_tool_returns_actionable_error(self):
        """If an adapter asks for test-execution grading but ships no exec-capable
        lifecycle tool, the runner returns an error that tells the author what's missing.
        """
        # We test the message shape; exercising the live RPC needs a full runner.
        # Read the source so the test fails if the actionable phrasing regresses.
        from pathlib import Path

        import tolokaforge.runner.service as service_mod

        src = Path(service_mod.__file__).read_text()
        # Required cues for an adapter author who hits this error:
        assert (
            "grading_method='test_execution'" in src
        ), "The 'no exec tool' error must name the field the adapter set."
        assert (
            "TaskDescription.agent_tools" in src
        ), "The 'no exec tool' error must point the adapter author to where to add the tool."
