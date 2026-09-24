"""A task-authored user-simulator system prompt.

``actors.user.prompt_template`` names a file whose text replaces the built-in
simulator prompt. The template carries the placeholder :data:`BACKSTORY_PLACEHOLDER`
exactly once, and the task's backstory is substituted there; the engine adds no
text of its own. That is what lets a task reproduce another harness's simulator
prompt byte for byte (a guidelines document followed by the scenario).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "BACKSTORY_PLACEHOLDER",
    "read_prompt_template",
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
