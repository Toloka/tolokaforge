"""A task-authored user-simulator system prompt.

``actors.user.prompt_template`` names a file whose text replaces the built-in
simulator prompt. The template carries the placeholder :data:`BACKSTORY_PLACEHOLDER`
exactly once, and the task's backstory is substituted there; the engine adds no
text of its own. That is what lets a task reproduce another harness's simulator
prompt byte for byte (a guidelines document followed by the scenario).

The loader and the conductor both call :func:`render_user_prompt_template`: the
loader so ``validate`` refuses a template that cannot be used, the conductor so a
task an adapter builds in Python is checked too, and so the simulator receives the
rendered prompt rather than a path or an unrendered template.
"""

from __future__ import annotations

from pathlib import Path

from tolokaforge.core.models.task_config import SIMULATOR_STOP_TOKEN, UserSimulatorConfig

__all__ = [
    "BACKSTORY_PLACEHOLDER",
    "render_prompt_template",
    "render_user_prompt_template",
]

BACKSTORY_PLACEHOLDER = "{backstory}"


def render_user_prompt_template(task_dir: Path, config: UserSimulatorConfig) -> str | None:
    """The system prompt *config*'s template renders to, or ``None`` without a template.

    The path is read relative to *task_dir*, like ``system_prompt``; an absolute
    path is read as is, which is what a project's ``task_defaults`` value becomes
    once the project loader anchors it to the project directory.

    Every listed stop token must appear in the rendered prompt: the engine listens
    for ``stop_tokens`` and the model sends what its prompt tells it to, so a token
    the prompt never names can never end a dialogue. The other direction — a token
    the prompt teaches but the list omits — cannot be read out of free text, so a
    task keeps both in one place.

    Raises:
        FileNotFoundError: No file is at the resolved path.
        ValueError: The config has no backstory to place, the placeholder is not
            there exactly once, or a listed stop token appears in neither the
            template nor the backstory.
    """
    if config.prompt_template is None:
        return None
    if config.backstory is None:
        raise ValueError(
            f"actors.user.prompt_template {config.prompt_template!r} places the task's "
            "backstory, and this user simulator has none. Declare actors.user.backstory."
        )
    template = _read_prompt_template(task_dir, config.prompt_template)
    prompt = render_prompt_template(template, config.backstory)
    unnamed = [token for token in config.stop_tokens if token not in prompt]
    if unnamed:
        raise ValueError(_unnamed_stop_tokens_message(config, unnamed))
    return prompt


def render_prompt_template(template: str, backstory: str) -> str:
    """*template* with *backstory* in place of its placeholder, and nothing else changed.

    ``str.replace`` rather than ``str.format``: a guidelines document may carry
    braces of its own, and they must reach the model untouched.
    """
    _require_one_placeholder(template, source="the prompt template")
    return template.replace(BACKSTORY_PLACEHOLDER, backstory)


def _read_prompt_template(task_dir: Path, path_text: str) -> str:
    path = task_dir / path_text
    if not path.is_file():
        raise FileNotFoundError(
            f"actors.user.prompt_template {path_text!r} resolves to {path}, which is not "
            "a file. A relative path is read from the task root, like system_prompt."
        )
    template = path.read_text(encoding="utf-8")
    _require_one_placeholder(template, source=str(path))
    return template


def _unnamed_stop_tokens_message(config: UserSimulatorConfig, unnamed: list[str]) -> str:
    problem = (
        f"stop_tokens lists {unnamed!r}, which the prompt rendered from "
        f"{config.prompt_template!r} never names, so the model is never told to send them."
    )
    if "stop_tokens" not in config.model_fields_set:
        return (
            f"{problem} The task declares no stop_tokens, so the list is the default "
            f"[{SIMULATOR_STOP_TOKEN!r}], the built-in prompt's token. Declare stop_tokens "
            "with the tokens the template teaches."
        )
    return f"{problem} Name each one in the template or the backstory, or drop it from the list."


def _require_one_placeholder(template: str, *, source: str) -> None:
    count = template.count(BACKSTORY_PLACEHOLDER)
    if count == 1:
        return
    raise ValueError(
        f"{source} carries {BACKSTORY_PLACEHOLDER!r} {count} times; a user-simulator prompt "
        "template must carry it exactly once, where the task's backstory is placed."
    )
