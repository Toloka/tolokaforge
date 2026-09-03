"""Every heuristic the stuck detector runs is satisfiable at every threshold
this repository ships, given a turn budget that admits it — and prose alone is
not one of them.

A stuck verdict ends the trial and short-circuits its grade, so a heuristic that
cannot fire is not a harmless spare part: it is a control that reads as live in
the config, in the docs and in the detector, and answers no question at all. The
claim is checked rather than asserted in a docstring, and both halves of it are
discovered rather than typed:

- the heuristic set is read off ``StuckDetector`` itself, so a predicate added
  with no driving case fails here rather than joining the tree unmeasured;
- the threshold set is read off the shipped sources — both config models and
  every ``stuck_heuristics`` block declared in in-tree YAML — so a threshold
  nobody could reach is a failure at the value somebody actually ships, not at
  one chosen to make the branch reachable.

What this does **not** claim is that a shipped pack reaches these conditions
inside its own turn budget. Each case is driven at a budget manufactured to
admit the threshold (see :func:`_drive`); whether a pack's authored ``max_turns``
leaves room for its threshold is a separate question, open as #1144.

Every case is driven end to end through :class:`TrialRunner` with a scripted
agent and a scripted user simulator, and asserted on the trajectory the trial
produced. No predicate is called directly and no detector is mocked, because
what is in question is whether a real dialogue can reach the condition — not
whether the condition evaluates as written.

Attribution is by input shape rather than by neutralising siblings: the
repeated-call agent emits no prose for the content heuristic to read, and the
prose agents emit no tool calls for the repeated-call heuristic to count.

The last lock runs the other way round. An agent that talks without acting is
the ordinary shape of a conversational task — the agent finishes, the user
thanks it, the agent answers — and it terminates ``max_turns``. Making that
shape a stuck verdict would auto-fail the majority of trials the corpus already
grades above zero, so it is locked as a permanent negative.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm.client import GenerationResult, UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import Message, TerminationReason, ToolCall, Trajectory
from tolokaforge.core.models.run_config import OrchestratorConfig
from tolokaforge.core.models.task_config import StuckHeuristicsDefaults
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Where an author declares a stuck-heuristics block in this tree: a project's
# task defaults and a run config. Both are globs, and a glob that stops matching
# would shrink the sweep to nothing without failing anything, so the discovery
# test below asserts they still find blocks.
_CONFIG_GLOBS = ("examples/**/project.yaml", "tests/data/configs/*.yaml")

_DETECTOR_PARAMS = frozenset(inspect.signature(StuckDetector).parameters)


class _ScriptedTurns:
    """Agent generate seam producing one turn's output from the turn number."""

    def __init__(self, script: Callable[[int], GenerationResult]) -> None:
        self._script = script
        self._turn = 0

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: Any = None,
    ) -> GenerationResult:
        self._turn += 1
        return self._script(self._turn)

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict] | None:
        return None


def _usage() -> Usage:
    return Usage(prompt_tokens=10, completion_tokens=5)


def _script_repeated_tool_call(turn: int) -> GenerationResult:
    """The identical call every turn, and no prose at all.

    The empty text is what keeps the attribution clean: with no trigrams to
    count, only the repeated-call heuristic can be what fires. The ids differ
    per turn because they are episode-unique; the signature the heuristic reads
    is the tool name and its arguments.
    """
    return GenerationResult(
        text="",
        tool_calls=[ToolCall(id=f"call-{turn}", name="search", arguments={"q": "same"})],
        usage=_usage(),
    )


def _script_looping_content(turn: int) -> GenerationResult:
    """One phrase, repeated twelve times inside a single message.

    That is what the content heuristic actually detects: repetition *within* one
    message, never repetition across turns (#1141). The input is chosen to match
    the heuristic as it behaves, so this locks that the condition is reachable —
    it does not endorse the condition, and #1141's fix will replace this case.
    """
    del turn
    return GenerationResult(
        text=" ".join(["let me try that again"] * 12),
        tool_calls=[],
        usage=_usage(),
    )


# Ten sentences sharing no trigram, cycled so no two messages inside the content
# heuristic's ten-message window are alike: this agent talks, and only talks.
_VARIED_PROSE = (
    "the quick brown fox jumps over lazy dogs",
    "alice went through mirror into wonderland today",
    "quantum computers solve problems exponentially faster",
    "three blind mice ran after farmer wife",
    "mars rover landed safely on red planet",
    "jazz musicians improvised melodies during late concert",
    "ocean waves crashed against rocky northern cliffs",
    "ancient pyramids stand tall beneath blazing sun",
    "software engineers debug production before monday release",
    "mountain climbers reached summit despite harsh weather",
)


def _script_varied_prose(turn: int) -> GenerationResult:
    """An agent that never calls a tool and never repeats itself."""
    return GenerationResult(
        text=_VARIED_PROSE[turn % len(_VARIED_PROSE)],
        tool_calls=[],
        usage=_usage(),
    )


# One agent behaviour per heuristic the detector runs. The keys are asserted
# against the detector's own predicates, so this table cannot fall behind it.
_DRIVING_CASES: dict[str, Callable[[int], GenerationResult]] = {
    "_has_repeated_tool_calls": _script_repeated_tool_call,
    "_has_looping_content": _script_looping_content,
}


def _thresholds(block: Mapping[str, Any]) -> dict[str, int]:
    """The detector kwargs a ``stuck_heuristics`` block ships.

    Keys the detector does not take (``enabled``) are dropped, and keys the
    block leaves out take the canonical task-scope default, which is what the
    loader's per-task merge leaves for them. Every block declared in this tree
    declares every knob, so nothing is inferred today.
    """
    canonical = {
        key: value
        for key, value in StuckHeuristicsDefaults().model_dump().items()
        if key in _DETECTOR_PARAMS
    }
    declared = {
        key: value
        for key, value in block.items()
        if key in _DETECTOR_PARAMS and isinstance(value, int) and not isinstance(value, bool)
    }
    return {**canonical, **declared}


def _stuck_blocks(node: Any) -> Iterator[Mapping[str, Any]]:
    """Every ``stuck_heuristics`` mapping anywhere in one loaded YAML document.

    Found by walking rather than by path, because the block is declared under
    ``task_defaults`` in a project and under ``orchestrator`` in a run config.
    """
    if isinstance(node, dict):
        block = node.get("stuck_heuristics")
        if isinstance(block, dict):
            yield block
        for value in node.values():
            yield from _stuck_blocks(value)
    elif isinstance(node, list):
        for item in node:
            yield from _stuck_blocks(item)


def _declared_in_tree() -> dict[str, dict[str, int]]:
    """Every stuck-heuristics block declared in in-tree YAML, by source file.

    A block declaring ``enabled: false`` is skipped: the conductor builds no
    detector for it, so it ships no threshold for anything to satisfy.
    """
    declared: dict[str, dict[str, int]] = {}
    for glob in _CONFIG_GLOBS:
        for path in sorted(_REPO_ROOT.glob(glob)):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for index, block in enumerate(_stuck_blocks(document)):
                if block.get("enabled") is False:
                    continue
                label = str(path.relative_to(_REPO_ROOT))
                declared[label if not index else f"{label} (block {index + 1})"] = _thresholds(
                    block
                )
    return declared


def _shipped_configurations() -> dict[str, dict[str, int]]:
    """Every stuck-heuristics configuration this repository ships, by source.

    The detector's own signature is not a source: it declares no default, and
    reading one from it would re-admit a re-added class default as a shipped
    value — the opposite of what its absence is for.
    """
    return {
        "task-scope model defaults": _thresholds(StuckHeuristicsDefaults().model_dump()),
        "run-side model defaults": _thresholds(OrchestratorConfig().stuck_heuristics.model_dump()),
        **_declared_in_tree(),
    }


_SHIPPED_CONFIGURATIONS = _shipped_configurations()


def _drive(script: Callable[[int], GenerationResult], thresholds: Mapping[str, int]) -> Trajectory:
    """Run one whole trial through :class:`TrialRunner` at *thresholds*.

    The turn budget is manufactured rather than read from any pack: four times
    the largest threshold under test, minimum 20. That is deliberate, and it is
    what scopes this module's claim. A budget below the threshold would end
    every trial ``max_turns`` and the sweep would report a heuristic as
    unsatisfiable when the real finding is that the budget was too small — two
    different defects, and only the first is this module's subject. Whether a
    shipped pack's own ``max_turns`` admits its threshold is #1144.
    """
    return TrialRunner(
        task_id="stuck-heuristics",
        trial_index=0,
        agent_client=_ScriptedTurns(script),
        user_simulator=UserSimulator(
            mode="scripted", scripted_flow=[{"default": "Please carry on."}]
        ),
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=max(20, 4 * max(thresholds.values())),
        episode_timeout_s=1200,
        stuck_detector=StuckDetector(**thresholds),
        interaction_mode="conversational",
    ).run("You are an agent.", "Do the task.")


def test_every_heuristic_the_detector_runs_has_a_driving_case() -> None:
    """A heuristic with no case would leave the sweep below silently partial."""
    predicates = {name for name in vars(StuckDetector) if name.startswith("_has_")}

    assert predicates, (
        "no heuristic was discovered on StuckDetector at all, so the sweep below "
        "drives nothing and passes vacuously"
    )
    assert predicates == set(_DRIVING_CASES), (
        "the heuristics StuckDetector runs and the cases driving them have "
        f"diverged: {sorted(predicates ^ set(_DRIVING_CASES))}. A heuristic with "
        "no driving case is unmeasured — nothing shows whether a real dialogue "
        "can reach it."
    )


def test_the_shipped_configuration_set_is_read_from_the_tree() -> None:
    """A glob that stops matching must fail here, not shrink the sweep."""
    declared = _declared_in_tree()

    assert declared, (
        "no stuck_heuristics block was found in any of "
        f"{list(_CONFIG_GLOBS)}, so the sweep runs at model defaults only and "
        "nothing checks the values this tree actually declares"
    )
    assert _SHIPPED_CONFIGURATIONS, "no shipped configuration was discovered from any source"


@pytest.mark.parametrize("heuristic", sorted(_DRIVING_CASES))
def test_each_heuristic_fires_at_every_shipped_configuration(heuristic: str) -> None:
    reached = {
        label: _drive(_DRIVING_CASES[heuristic], thresholds)
        for label, thresholds in _SHIPPED_CONFIGURATIONS.items()
    }
    missed = [
        f"{label} {dict(_SHIPPED_CONFIGURATIONS[label])} ended "
        f"{trajectory.termination_reason.value}"
        for label, trajectory in reached.items()
        if trajectory.termination_reason is not TerminationReason.STUCK_DETECTED
    ]

    assert not missed, (
        f"{heuristic} never fired at a configuration this repository ships: "
        f"{missed}. "
        "A stuck verdict ends the trial and short-circuits its grade, so a "
        "heuristic no shipped threshold can reach is a control that answers "
        "nothing while reading as live."
    )
    assert all(trajectory.metrics.stuck_detected for trajectory in reached.values()), (
        f"{heuristic} terminated a trial stuck_detected without setting "
        "metrics.stuck_detected, so the run aggregate's stuck_rate would not count it"
    )


@pytest.mark.parametrize(
    "thresholds",
    [pytest.param(thresholds, id=label) for label, thresholds in _SHIPPED_CONFIGURATIONS.items()],
)
def test_an_agent_that_only_talks_runs_out_of_turns_rather_than_stuck(
    thresholds: Mapping[str, int],
) -> None:
    """Prose without tool calls is how a polite dialogue closes, not a stall.

    Whether an agent must *act* is a per-task assertion — ``transcript_rules``
    in the task's ``grading.yaml`` — and a harness-global heuristic answering it
    would auto-fail trials that are graded above zero today.
    """
    trajectory = _drive(_script_varied_prose, thresholds)

    assert trajectory.termination_reason is TerminationReason.MAX_TURNS, (
        "an agent that talks without acting was terminated "
        f"{trajectory.termination_reason.value} at {dict(thresholds)}. That shape "
        "is the ordinary end of a conversational task, and a stuck verdict on it "
        "scores the trial 0.0 without ever checking the state the agent left."
    )
    assert trajectory.metrics.stuck_detected is False
