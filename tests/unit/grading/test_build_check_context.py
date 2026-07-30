"""Lock the shared :func:`build_check_context` state-shape transform.

Both grading paths — host
:class:`~tolokaforge.core.grading.combine.GradingEngine` and runner
:class:`~tolokaforge.runner.service.RunnerServiceImpl` — build their
:class:`CheckContext` through this one helper so a check reads identical
evidence from either. These parametrised cases pin the precedence rule
verbatim from ``combine.py``:

* ``initial_state.data`` = the initial ``json_db`` dict when it is one,
  else ``{}``.
* ``final_state.data`` picks from ``final_env_state`` by precedence —
  the ``"agent"`` dict wins, then the ``"db"`` dict, else the flat map.
* When ``final_env_state`` carries a ``"filesystem"`` key and the chosen
  level lacks one, the filesystem is merged into ``final_state.data``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.checks_helpers import build_check_context
from tolokaforge.core.grading.checks_interface import TaskContext, Transcript

pytestmark = pytest.mark.unit


def _task() -> TaskContext:
    return TaskContext(task_id="t1", task_name="t1", domain="general")


class TestInitialStateResolution:
    """``initial_state.data`` is the initial ``json_db`` dict when one is
    supplied, else empty. Non-dict values (str path, ``None``) do not
    surface as data — the host reads ``.json_db``, not ``.tables``, and a
    path-style value would need file I/O which the helper deliberately
    does not perform.
    """

    def test_dict_initial_state_is_used_as_data(self) -> None:
        initial = {"users": [{"id": "u1"}], "orders": []}

        ctx = build_check_context(
            initial_state_json_db=initial,
            final_env_state={},
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.initial_state.data == initial

    def test_none_initial_state_yields_empty_data(self) -> None:
        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={},
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.initial_state.data == {}


@pytest.mark.parametrize(
    ("final_env_state", "expected"),
    [
        pytest.param(
            {
                "agent": {"counter": 5, "orders": [{"id": "o1"}]},
                "db": {"counter": 999},  # loses to agent
                "user": {"noise": True},
            },
            {"counter": 5, "orders": [{"id": "o1"}]},
            id="agent_wins_over_db_and_flat",
        ),
        pytest.param(
            {"db": {"counter": 7, "status": "done"}, "user": {"noise": True}},
            {"counter": 7, "status": "done"},
            id="db_used_when_no_agent",
        ),
        pytest.param(
            {"counter": 3, "status": "pending"},
            {"counter": 3, "status": "pending"},
            id="flat_state_used_when_no_agent_no_db",
        ),
        pytest.param(
            {"agent": None, "db": {"counter": 1}},
            {"counter": 1},
            id="agent_key_present_but_not_dict_falls_through_to_db",
        ),
    ],
)
def test_final_state_precedence(final_env_state: dict, expected: dict) -> None:
    ctx = build_check_context(
        initial_state_json_db=None,
        final_env_state=final_env_state,
        transcript=Transcript(messages=[]),
        task=_task(),
    )

    assert ctx.final_state.data == expected


class TestFilesystemPassthrough:
    """A ``"filesystem"`` key at the top of ``final_env_state`` is merged
    into whatever level ``final_state.data`` resolved to (agent / db /
    flat) so a check can read agent-produced files alongside DB rows.
    """

    def test_filesystem_merged_into_agent_level(self) -> None:
        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={
                "agent": {"counter": 5},
                "filesystem": {"/work/out.txt": "hello"},
            },
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.final_state.data == {
            "counter": 5,
            "filesystem": {"/work/out.txt": "hello"},
        }

    def test_filesystem_merged_into_db_level(self) -> None:
        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={
                "db": {"counter": 5},
                "filesystem": {"/work/out.txt": "hello"},
            },
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.final_state.data == {
            "counter": 5,
            "filesystem": {"/work/out.txt": "hello"},
        }

    def test_filesystem_merged_into_flat_state(self) -> None:
        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={"counter": 3, "filesystem": {"/work/out.txt": "hi"}},
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.final_state.data == {
            "counter": 3,
            "filesystem": {"/work/out.txt": "hi"},
        }

    def test_existing_filesystem_at_chosen_level_is_not_overwritten(self) -> None:
        """A pre-existing ``filesystem`` at the chosen level wins; the
        top-level ``filesystem`` does NOT clobber it. This mirrors the
        host: the merge is a fill-in, not a replace."""
        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={
                "agent": {"filesystem": {"/work/a.txt": "agent-level"}},
                "filesystem": {"/work/b.txt": "top-level"},
            },
            transcript=Transcript(messages=[]),
            task=_task(),
        )

        assert ctx.final_state.data == {"filesystem": {"/work/a.txt": "agent-level"}}


class TestTranscriptAndTaskArePassedThrough:
    """The helper owns only the state-shape transform — ``transcript``
    and ``task`` travel through unchanged; each grading path builds them
    from its own source (rich ``Trajectory`` on the host, wire message
    dicts on the runner)."""

    def test_transcript_and_task_are_verbatim(self) -> None:
        transcript = Transcript(messages=[])
        task = TaskContext(task_id="t42", task_name="t42", domain="airline")

        ctx = build_check_context(
            initial_state_json_db=None,
            final_env_state={},
            transcript=transcript,
            task=task,
        )

        assert ctx.transcript is transcript
        assert ctx.task is task
