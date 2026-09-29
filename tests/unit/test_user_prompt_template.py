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
    render_prompt_template,
    render_user_prompt_template,
)
from tolokaforge.core.llm import UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.models.task_config import UserSimulatorConfig

pytestmark = pytest.mark.unit

# A guidelines document followed by a scenario envelope — the shape the τ³-bench
# harness builds its simulator prompt in. Braces in the guidelines must reach the
# model untouched.
_GUIDELINES = (
    '# User simulation\nStay in role. Reply as JSON like {"ok": true} only when asked.\n'
    "When your goal is met, send '###STOP###'."
)
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


def _templated(path: str, **fields: object) -> UserSimulatorConfig:
    return UserSimulatorConfig(mode="llm", backstory=_BACKSTORY, prompt_template=path, **fields)


class TestRenderUserPromptTemplate:
    def test_the_path_is_read_relative_to_the_task_root(self, tmp_path: Path) -> None:
        (tmp_path / "sim").mkdir()
        (tmp_path / "sim" / "prompt.md").write_text(_TEMPLATE, encoding="utf-8")

        assert render_user_prompt_template(tmp_path, _templated("sim/prompt.md")) == _RENDERED

    def test_an_absolute_path_is_read_as_is(self, tmp_path: Path) -> None:
        """A project's ``task_defaults`` value reaches the task anchored to the
        project directory, as an absolute path."""
        shared = tmp_path / "project" / "shared" / "prompt.md"
        shared.parent.mkdir(parents=True)
        shared.write_text(_TEMPLATE, encoding="utf-8")

        rendered = render_user_prompt_template(tmp_path / "task", _templated(str(shared)))

        assert rendered == _RENDERED

    def test_no_template_renders_nothing(self, tmp_path: Path) -> None:
        config = UserSimulatorConfig(mode="llm", backstory=_BACKSTORY)

        assert render_user_prompt_template(tmp_path, config) is None

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="read from the task root"):
            render_user_prompt_template(tmp_path, _templated("sim/prompt.md"))

    def test_a_file_without_the_placeholder_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text(_GUIDELINES, encoding="utf-8")
        with pytest.raises(ValueError, match="exactly once"):
            render_user_prompt_template(tmp_path, _templated("prompt.md"))

    def test_an_undeclared_list_is_named_as_the_default(self, tmp_path: Path) -> None:
        """A template that teaches its own token, on a task that declares no
        ``stop_tokens``, is refused with the remedy that applies: declare the list."""
        (tmp_path / "prompt.md").write_text(
            "Send '###DONE###' when finished.\n\n{backstory}", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="declares no stop_tokens.*Declare stop_tokens"):
            render_user_prompt_template(tmp_path, _templated("prompt.md"))

    def test_a_declared_list_is_told_to_name_or_drop_the_token(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text(_TEMPLATE, encoding="utf-8")
        config = _templated("prompt.md", stop_tokens=["###STOP###", "###TRANSFER###"])
        with pytest.raises(ValueError, match="or drop it from the list"):
            render_user_prompt_template(tmp_path, config)


class TestSimulatorPrompt:
    def test_the_task_prompt_replaces_the_built_in_prompt_whole(self) -> None:
        sim = UserSimulator(mode="llm", backstory=_BACKSTORY, system_prompt=_RENDERED)

        assert sim._build_system_prompt() == _RENDERED

    def test_tool_schemas_add_no_guidance_to_a_task_prompt(self) -> None:
        """The built-in tool-guidance block is the engine's text; a template owns
        its own guidance, so holding tools changes nothing in the prompt."""
        sim = UserSimulator(
            mode="llm", backstory=_BACKSTORY, system_prompt=_RENDERED, tool_schemas=[{}]
        )

        assert sim._build_system_prompt() == _RENDERED

    def test_the_task_prompt_is_the_prompt_a_generation_is_sent(self) -> None:
        sim = UserSimulator(
            mode="llm",
            llm_config=ModelConfig(provider="mock", name="user-sim-mock"),
            backstory=_BACKSTORY,
            system_prompt=_RENDERED,
        )
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        sim.reply([Message(role=MessageRole.ASSISTANT, content="Hi!", ts=ts)])

        assert sim.last_system_prompt == _RENDERED

    def test_a_scripted_simulator_refuses_a_task_prompt(self) -> None:
        """A scripted simulator never sends a prompt, so accepting one would
        record a prompt that no request carried."""
        with pytest.raises(ValueError, match="sends no prompt"):
            UserSimulator(mode="scripted", backstory=_BACKSTORY, system_prompt=_RENDERED)

    def test_no_task_prompt_keeps_the_built_in_prompt(self) -> None:
        with_template = UserSimulator(mode="llm", backstory=_BACKSTORY, system_prompt=_RENDERED)
        without = UserSimulator(mode="llm", backstory=_BACKSTORY)

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
            template="Send '###DONE###' when finished.\n\n<scenario>\n{backstory}\n</scenario>",
        )

        assert load_task_yaml(task_path)[0].resolve_user_simulator().stop_tokens == ["###DONE###"]

    def test_a_stop_token_the_rendered_prompt_never_names_is_refused(self, tmp_path: Path) -> None:
        """The template teaches ###STOP### only, so listening for ###TRANSFER### could
        never end a dialogue."""
        task_path = _write_task(
            tmp_path,
            {
                "backstory": _BACKSTORY,
                "prompt_template": "sim/prompt.md",
                "stop_tokens": ["###STOP###", "###TRANSFER###"],
            },
        )
        with pytest.raises(ValueError, match=r"\['###TRANSFER###'\].*never names"):
            load_task_yaml(task_path)

    def test_a_stop_token_the_backstory_names_counts_as_named(self, tmp_path: Path) -> None:
        """The check reads the rendered prompt, so the backstory can teach a token."""
        task_path = _write_task(
            tmp_path,
            {
                "backstory": _BACKSTORY + " If the agent transfers you, send ###TRANSFER###.",
                "prompt_template": "sim/prompt.md",
                "stop_tokens": ["###STOP###", "###TRANSFER###"],
            },
        )

        assert load_task_yaml(task_path)[0].resolve_user_simulator().stop_tokens == [
            "###STOP###",
            "###TRANSFER###",
        ]
