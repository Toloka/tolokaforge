"""``actors.user.prompt_template``: a task-authored simulator prompt.

The template replaces the built-in prompt whole. The engine substitutes the
backstory at the one placeholder and adds nothing else, so a task can reproduce
another harness's simulator prompt byte for byte. Every way a template could
fail to render is refused at load, before any trial is built.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.actors.prompt_template import (
    read_prompt_template,
    render_prompt_template,
)
from tolokaforge.core.llm import UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit

# A guidelines document followed by a scenario envelope — the shape the τ³-bench
# harness builds its simulator prompt in. Braces in the guidelines must reach the
# model untouched.
_GUIDELINES = '# User simulation\nStay in role. Reply as JSON like {"ok": true} only when asked.'
_TEMPLATE = _GUIDELINES + "\n\n<scenario>\n{backstory}\n</scenario>"
_BACKSTORY = "Instructions:\n\tYou want to move your booking to Friday."
_RENDERED = _GUIDELINES + "\n\n<scenario>\n" + _BACKSTORY + "\n</scenario>"


class TestRender:
    def test_the_backstory_lands_at_the_placeholder_and_nothing_else_changes(self) -> None:
        assert render_prompt_template(_TEMPLATE, _BACKSTORY) == _RENDERED

    @pytest.mark.parametrize(
        ("template", "count"),
        [("no placeholder here", 0), ("{backstory} and again {backstory}", 2)],
    )
    def test_a_template_without_exactly_one_placeholder_is_refused(
        self, template: str, count: int
    ) -> None:
        with pytest.raises(ValueError, match=f"{count} times"):
            render_prompt_template(template, _BACKSTORY)


class TestRead:
    def test_the_path_is_read_relative_to_the_task_root(self, tmp_path: Path) -> None:
        (tmp_path / "sim").mkdir()
        (tmp_path / "sim" / "prompt.md").write_text(_TEMPLATE, encoding="utf-8")

        assert read_prompt_template(tmp_path, "sim/prompt.md") == _TEMPLATE

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="relative to the task root"):
            read_prompt_template(tmp_path, "sim/prompt.md")

    def test_a_file_without_the_placeholder_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text(_GUIDELINES, encoding="utf-8")
        with pytest.raises(ValueError, match="exactly once"):
            read_prompt_template(tmp_path, "prompt.md")


class TestSimulatorPrompt:
    def test_the_template_replaces_the_built_in_prompt_whole(self) -> None:
        sim = UserSimulator(mode="llm", backstory=_BACKSTORY, prompt_template=_TEMPLATE)

        assert sim._build_system_prompt() == _RENDERED

    def test_tool_schemas_add_no_guidance_to_a_template(self) -> None:
        """The built-in tool-guidance block is the engine's text; a template owns
        its own guidance, so holding tools changes nothing in the prompt."""
        sim = UserSimulator(
            mode="llm", backstory=_BACKSTORY, prompt_template=_TEMPLATE, tool_schemas=[{}]
        )

        assert sim._build_system_prompt() == _RENDERED

    def test_the_rendered_template_is_the_prompt_a_generation_is_sent(self) -> None:
        sim = UserSimulator(
            mode="llm",
            llm_config=ModelConfig(provider="mock", name="user-sim-mock"),
            backstory=_BACKSTORY,
            prompt_template=_TEMPLATE,
        )
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        sim.reply([Message(role=MessageRole.ASSISTANT, content="Hi!", ts=ts)])

        assert sim.last_system_prompt == _RENDERED

    def test_a_template_without_a_backstory_is_refused(self) -> None:
        with pytest.raises(ValueError, match="none to place"):
            UserSimulator(mode="llm", backstory=None, prompt_template=_TEMPLATE)

    def test_no_template_keeps_the_built_in_prompt(self) -> None:
        with_template = UserSimulator(backstory=_BACKSTORY, prompt_template=_TEMPLATE)
        without = UserSimulator(backstory=_BACKSTORY)

        assert without._build_system_prompt() != with_template._build_system_prompt()
        assert without._build_system_prompt().startswith(
            "You are a user interacting with an agent."
        )


def _write_task(task_dir: Path, user: dict, *, template: str | None = _TEMPLATE) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    if template is not None:
        (task_dir / "sim").mkdir(exist_ok=True)
        (task_dir / "sim" / "prompt.md").write_text(template, encoding="utf-8")
    task_path = task_dir / "task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "task_id": "templated",
                "description": "d",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "actors": {"user": user},
            }
        ),
        encoding="utf-8",
    )
    return task_path


class TestLoad:
    def test_a_usable_template_loads_and_resolves(self, tmp_path: Path) -> None:
        task_path = _write_task(
            tmp_path, {"backstory": _BACKSTORY, "prompt_template": "sim/prompt.md"}
        )

        sim = load_task_yaml(task_path)[0].resolve_user_simulator()

        assert sim.prompt_template == "sim/prompt.md"

    def test_a_missing_template_file_is_refused_at_load(self, tmp_path: Path) -> None:
        task_path = _write_task(
            tmp_path, {"backstory": _BACKSTORY, "prompt_template": "sim/prompt.md"}, template=None
        )
        with pytest.raises(FileNotFoundError, match="prompt_template"):
            load_task_yaml(task_path)

    def test_a_template_without_the_placeholder_is_refused_at_load(self, tmp_path: Path) -> None:
        task_path = _write_task(
            tmp_path,
            {"backstory": _BACKSTORY, "prompt_template": "sim/prompt.md"},
            template=_GUIDELINES,
        )
        with pytest.raises(ValueError, match="exactly once"):
            load_task_yaml(task_path)

    def test_a_template_without_a_backstory_is_refused_at_load(self, tmp_path: Path) -> None:
        task_path = _write_task(tmp_path, {"prompt_template": "sim/prompt.md"})
        with pytest.raises(ValueError, match="no backstory"):
            load_task_yaml(task_path)

    def test_a_template_on_a_scripted_simulator_is_refused_at_load(self, tmp_path: Path) -> None:
        task_path = _write_task(
            tmp_path,
            {"mode": "scripted", "backstory": _BACKSTORY, "prompt_template": "sim/prompt.md"},
        )
        with pytest.raises(ValueError, match="sends no prompt"):
            load_task_yaml(task_path)

    def test_a_template_may_name_its_own_stop_tokens(self, tmp_path: Path) -> None:
        """The built-in prompt's ``###STOP###`` requirement does not bind a task
        that authors the prompt itself."""
        task_path = _write_task(
            tmp_path,
            {
                "backstory": _BACKSTORY,
                "prompt_template": "sim/prompt.md",
                "stop_tokens": ["###DONE###"],
            },
        )

        assert load_task_yaml(task_path)[0].resolve_user_simulator().stop_tokens == ["###DONE###"]
