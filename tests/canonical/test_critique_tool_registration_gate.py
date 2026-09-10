"""Canonical gate: ``critique`` is registered iff the episode is agentic-and-enabled.

``ScriptedLLMClient.generate(...)`` ignores its ``tools`` argument, so driving a
scripted episode to completion cannot distinguish "the tool schema list omits
``critique``" from "the tool schema list includes it but the script never calls
it" — both replay identically. Every test here therefore inspects the schema
list a judge kind actually constructs (``ToolRegistry.get_schemas()`` for the
non-agentic kinds' shared ``build_judge_registry`` call site, ``_EpisodeSetup
.tool_schemas`` for the agentic kind's ``_build_episode_setup``) rather than
running an episode or checking registry membership.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge import build_judge_registry
from tolokaforge.core.grading.judge_kinds import agentic
from tolokaforge.core.grading.rubric import CRITIQUE_TOOL_NAME
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.canonical

_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


def _rubric() -> Rubric:
    return Rubric(
        criteria=[Criterion(id="c0", description="criterion 0", kind="binary", weight=1.0)]
    )


class _StubJudgeModelProvider:
    """Scripted ``JudgeModelProvider`` returning a preloaded client from every ``build``."""

    def __init__(self, client: ScriptedLLMClient) -> None:
        self._client = client

    def build(self, model_config: ModelConfig) -> ScriptedLLMClient:  # noqa: ARG002
        return self._client


def _schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {schema["function"]["name"] for schema in schemas}


def test_critique_absent_from_non_agentic_registry() -> None:
    """``build_judge_registry`` never registers ``critique``.

    Both ``single_shot_rubric`` and ``chunked_rubric`` build their tool
    registry exclusively through ``LLMJudge.run``'s call to
    ``build_judge_registry`` (``judge.py:632``) — neither kind has its own
    call site, so one direct call against representative inputs proves the
    invariant for both kind names.
    """
    registry, _, _, _ = build_judge_registry(
        _rubric(),
        db_reader=None,
        kb_search=None,
        extra_read_tools=None,
        workspace_dir=None,
        disable_knowledge_search=False,
        logger=StructuredLogger(name="test-registration-gate"),
    )
    names = _schema_names(registry.get_schemas(sanitize=False))
    assert CRITIQUE_TOOL_NAME not in names, f"critique leaked into non-agentic registry: {names}"


def _build_agentic_setup(*, kind_config_resolved: agentic._AgenticKindConfig) -> Any:
    client = ScriptedLLMClient(["draft complete"])
    return agentic._build_episode_setup(
        rubric=_rubric(),
        agent_system_prompt="you are an agent",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=None,
        kb_search=None,
        workspace_dir=None,
        extra_read_tools=[],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        judge_model_provider=_StubJudgeModelProvider(client),
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        kind_config_resolved=kind_config_resolved,
        logger=StructuredLogger(name="test-registration-gate"),
    )


def test_critique_present_in_agentic_registry_by_default() -> None:
    """Default ``kind_config`` (``enable_critique_tool=True``) puts ``critique``
    in ``_EpisodeSetup.tool_schemas`` — the field the judge loop is actually
    constructed with, not a proxy for it."""
    setup = _build_agentic_setup(kind_config_resolved=agentic._AgenticKindConfig())
    names = _schema_names(setup.tool_schemas)
    assert CRITIQUE_TOOL_NAME in names, f"critique missing from default agentic setup: {names}"


def test_critique_absent_when_enable_critique_tool_false() -> None:
    """``enable_critique_tool=False`` keeps ``critique`` out of
    ``_EpisodeSetup.tool_schemas`` — registering it after the snapshot would
    pass a registry-membership check but fail this one."""
    setup = _build_agentic_setup(
        kind_config_resolved=agentic._AgenticKindConfig(enable_critique_tool=False)
    )
    names = _schema_names(setup.tool_schemas)
    assert CRITIQUE_TOOL_NAME not in names, f"critique leaked into disabled agentic setup: {names}"
