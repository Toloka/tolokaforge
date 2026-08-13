"""``###STOP###`` is the user simulator's exit token, and no agent prompt carries it.

The token instructs the *simulator* when to close the dialogue. The agent is never
asked for it and never checked for it — its completion is structural, decided by the
turn policy (ADR-0032). This guard holds the first half of that separation over the
shipped corpus: every example pack is loaded and its agent system prompt is built the
way a run builds it, and none of them contains the token.

The guard measures :func:`build_system_prompt` — the *pre-enrichment* prompt, the
string the task's own authoring surfaces produce. A marker a
``SystemPromptPolicy.enrich`` adds on the way to the wire is outside its reach and is
not claimed here.

The pack list is asserted against discovery rather than merely being non-empty: a
walk that silently stopped finding packs would otherwise pass over the empty set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.core.system_prompt import build_system_prompt

pytestmark = pytest.mark.canonical

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "examples"

#: The exit token, spelled here rather than imported: no engine constant holds it for
#: the agent side, which is the point of this file.
_SIMULATOR_EXIT_TOKEN = "###STOP###"

#: The two example task files no :class:`TaskConfig` can be built from — upstream
#: terminal-bench task descriptors carrying an ``instruction`` block and no
#: ``task_id``. Named, and each one's unloadability re-checked below, so the list
#: cannot become the place a pack that *does* load is parked.
_UNLOADABLE_TASK_FILES = (
    _EXAMPLES / "terminal_bench" / "fix-airline-segmentation" / "task.yaml",
    _EXAMPLES / "terminal_bench" / "fix-billing-holds" / "task.yaml",
)


def _enclosing_project(task_yaml: Path) -> Path | None:
    """The ``project.yaml`` whose layer this task loads under, or ``None``.

    Bounded by the corpus root: an unbounded walk would go on to ask the repository
    root and the filesystem above it.
    """
    for directory in task_yaml.parents:
        candidate = directory / "project.yaml"
        if candidate.exists():
            return candidate
        if directory == _EXAMPLES:
            return None
    return None


def _project_layer(task_yaml: Path) -> dict[str, Any] | None:
    """The ``task_defaults`` layer beneath this task, or ``None`` for no project."""
    project_yaml = _enclosing_project(task_yaml)
    if project_yaml is None:
        return None
    return load_project_config(project_yaml).task_defaults.model_dump(exclude_defaults=True) or None


def _load(task_yaml: Path) -> tuple[TaskConfig, Path] | None:
    """The task and its effective directory, or ``None`` when nothing loads."""
    try:
        return load_task_yaml(task_yaml, project_task_defaults=_project_layer(task_yaml))
    except (ValidationError, RuntimeError):
        return None


@pytest.fixture(scope="module")
def agent_prompts() -> dict[Path, str]:
    """The agent system prompt every loadable example pack builds."""
    prompts: dict[Path, str] = {}
    for task_yaml in sorted(_EXAMPLES.rglob("task.yaml")):
        loaded = _load(task_yaml)
        if loaded is None:
            continue
        task, task_dir = loaded
        assert task.system_prompt != "__adapter__", (
            f"{task_yaml} routes its system prompt through its adapter, a branch this "
            "guard drives with adapter=None and therefore does not measure. Build the "
            "pack's adapter here so the prompt under test is the one a run sends."
        )
        prompts[task_yaml] = build_system_prompt(task=task, task_dir=task_dir, adapter=None)
    return prompts


def test_the_driven_packs_are_the_example_corpus_minus_two_unloadable_files(
    agent_prompts: dict[Path, str],
) -> None:
    """Which packs the guard below answers for, pinned to discovery.

    Both exclusions are re-derived from the files rather than trusted: a named file
    that does load is a pack hidden from the guard, and a task file dropped on
    neither ground would leave the walk measuring an unstated subset.
    """
    discovered = set(_EXAMPLES.rglob("task.yaml"))

    assert set(agent_prompts) == discovered - set(_UNLOADABLE_TASK_FILES)
    for task_yaml in _UNLOADABLE_TASK_FILES:
        assert task_yaml.exists(), f"{task_yaml} is excluded by name and does not exist"
        assert _load(task_yaml) is None, (
            f"{task_yaml} loads as a TaskConfig, so excluding it by name hides a pack "
            "from the prompt guard"
        )


def test_no_example_pack_builds_an_agent_prompt_carrying_the_exit_token(
    agent_prompts: dict[Path, str],
) -> None:
    """The agent is never told the simulator's exit token.

    A pack whose prompt carried it would be instructing the agent in a token the
    engine reads from nobody's output, and would make the agent's completion look
    like something it can announce in prose.
    """
    carrying = sorted(
        str(task_yaml.relative_to(_REPO))
        for task_yaml, prompt in agent_prompts.items()
        if _SIMULATOR_EXIT_TOKEN in prompt
    )

    assert carrying == [], (
        f"build_system_prompt puts {_SIMULATOR_EXIT_TOKEN} into the agent's "
        f"pre-enrichment system prompt for: {carrying}. That token is the user "
        "simulator's exit condition, read only from simulator replies; the agent's "
        "completion is structural (ADR-0032), so instructing the agent in it promises "
        "a signal nothing consumes."
    )
