"""A task-authored user-simulator system prompt.

``actors.user.prompt_template`` names a file whose text replaces the built-in
simulator prompt. The template carries the placeholder :data:`BACKSTORY_PLACEHOLDER`
exactly once, and the task's backstory is substituted there; the engine adds no
text of its own. That is what lets a task reproduce another harness's simulator
prompt byte for byte (a guidelines document followed by the scenario).
"""

from __future__ import annotations

from pathlib import Path

from tolokaforge.core.models.task_config import UserSimulatorConfig

__all__ = [
    "BACKSTORY_PLACEHOLDER",
    "read_prompt_template",
    "read_user_prompt_template",
    "render_prompt_template",
]

BACKSTORY_PLACEHOLDER = "{backstory}"


def read_prompt_template(task_dir: Path, relative_path: str) -> str:
    """The template text at *relative_path* under *task_dir*, checked for use.

    Raises:
        FileNotFoundError: No file is at the resolved path.
        ValueError: The file does not carry the placeholder exactly once.
    """
    path = task_dir / relative_path
    if not path.is_file():
        raise FileNotFoundError(
            f"actors.user.prompt_template {relative_path!r} resolves to {path}, which is not "
            "a file. The path is read relative to the task root, like system_prompt."
        )
    template = path.read_text(encoding="utf-8")
    _require_one_placeholder(template, source=str(path))
    return template


def read_user_prompt_template(task_dir: Path, config: UserSimulatorConfig) -> str | None:
    """The template *config* names, checked against the actor it prompts, or ``None``.

    Beyond :func:`read_prompt_template`, every listed stop token must appear in the
    prompt the template renders to: the engine listens for ``stop_tokens`` and the
    model sends what its prompt tells it to, so a token the prompt never names can
    never end a dialogue. The other direction — a token the prompt teaches but the
    list omits — cannot be read out of free text, so a task keeps both in one place.

    Raises:
        FileNotFoundError: No file is at the resolved path.
        ValueError: The placeholder is not there exactly once, or a listed stop
            token appears in neither the template nor the backstory.
    """
    if config.prompt_template is None:
        return None
    template = read_prompt_template(task_dir, config.prompt_template)
    prompt = render_prompt_template(template, config.backstory or "")
    unnamed = [token for token in config.stop_tokens if token not in prompt]
    if unnamed:
        raise ValueError(
            f"stop_tokens lists {unnamed!r}, which the prompt rendered from "
            f"{config.prompt_template!r} never names, so the model is never told to send "
            "them. Name each one in the template or the backstory, or drop it from the list."
        )
    return template


def render_prompt_template(template: str, backstory: str) -> str:
    """*template* with *backstory* in place of its placeholder, and nothing else changed.

    ``str.replace`` rather than ``str.format``: a guidelines document may carry
    braces of its own, and they must reach the model untouched.
    """
    _require_one_placeholder(template, source="the prompt template")
    return template.replace(BACKSTORY_PLACEHOLDER, backstory)


def _require_one_placeholder(template: str, *, source: str) -> None:
    count = template.count(BACKSTORY_PLACEHOLDER)
    if count == 1:
        return
    raise ValueError(
        f"{source} carries {BACKSTORY_PLACEHOLDER!r} {count} times; a user-simulator prompt "
        "template must carry it exactly once, where the task's backstory is placed."
    )
