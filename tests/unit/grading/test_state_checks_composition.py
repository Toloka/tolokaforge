"""How each substrate folds a state hash and JSONPath assertions into one component.

Drives the real :class:`~tolokaforge.core.grading.combine.GradingEngine` and the
real ``RegisterTrial`` rather than the composer directly (that arithmetic is locked
in ``test_state_composition.py``), because what these cases pin is the *routing*:
what state each half is evaluated against, when a source contributes nothing, what
happens when a hash was configured and could not be computed, and that both substrates
reach one shared predicate when they decide a config is ungradeable.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.golden_replay import GoldenReplayError
from tolokaforge.core.grading.state_checks import (
    consistent_hash,
    to_hashable,
)
from tolokaforge.core.grading.state_composition import (
    HASH_SOURCE_KEYS,
    INERT_HASH_WEIGHT_REASON,
    MISSING_HASH_WEIGHT_MESSAGE,
)
from tolokaforge.core.models import (
    GradingConfig,
    InitialStateConfig,
    StateChecksConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import RunnerStateChecksConfig

pytestmark = pytest.mark.unit

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)

# A wrapped final environment state, the shape every grading path receives.
_DB_STATE = {"widgets": [{"id": "W1", "status": "closed"}, {"id": "W2", "status": "open"}]}
_FINAL_ENV_STATE = {"agent": {}, "user": {}, "db": _DB_STATE}

# Two conventionally rooted assertions, exactly one of them satisfied.
_HALF_SATISFIED_JSONPATHS = [
    {"path": "$.db.widgets[0].status", "equals": "closed", "description": "W1 closed"},
    {"path": "$.db.widgets[1].status", "equals": "closed", "description": "W2 closed"},
]

_MATCHING_HASH = consistent_hash(to_hashable(_DB_STATE))
_MISMATCHING_HASH = "0" * 64

#: A ``shop_orders_02`` action that resolves, runs whole, and leaves the initial state
#: untouched — so a replay over that pack reaches a verdict without the pack's own
#: golden path having to be authored into the case.
_GOLDEN_ACTION_THAT_RUNS_WHOLE = [{"name": "get_customer", "kwargs": {"customer_id": "C-101"}}]

_CUSTOM_CHECKS_SENTINEL = "custom_checks_ran"

_RECORDING_CHECKS_PY = f'''\
"""A custom-checks suite whose only job is to record that it was reached."""

from pathlib import Path

from tolokaforge.core.grading.checks_interface import CheckContext, CheckPassed, check, init


@init(interface_version="1.0")
def setup(ctx: CheckContext):
    Path(__file__).with_name("{_CUSTOM_CHECKS_SENTINEL}").write_text("ran")


@check
def the_suite_ran():
    return CheckPassed("ran")
'''


def _trajectory() -> Trajectory:
    return Trajectory(
        task_id="state-composition",
        trial_index=0,
        start_ts=_TS,
        end_ts=_TS,
        status=TrialStatus.COMPLETED,
        messages=[],
    )


def _engine(state_checks: dict, **engine_kwargs) -> GradingEngine:
    config = GradingConfig(
        **{"state_checks": state_checks, "combine": {"weights": {"state_checks": 1.0}}}
    )
    return GradingEngine(grading_config=config, **engine_kwargs)


def _grade(state_checks: dict, *, final_env_state: dict | None = None, **engine_kwargs):
    return _engine(state_checks, **engine_kwargs).grade_trajectory(
        _trajectory(), final_env_state if final_env_state is not None else _FINAL_ENV_STATE
    )


class TestJsonpathStateRoot:
    """JSONPath assertions read the whole final env state; the hash reads the
    unwrapped database inside it.

    One case guards both halves of that split. The hash is pinned to the
    unwrapped ``db`` level, so hashing the wrapped state scores ``0.0``; the
    assertions are rooted ``$.db.…``, so evaluating them against the unwrapped
    level reports ``Path not found`` and scores ``0.0``. Either root swapped moves
    every expected number below.
    """

    @pytest.mark.parametrize(
        ("hash_weight", "expected"),
        [(0.0, 0.5), (0.25, 0.625), (0.6, 0.8)],
    )
    def test_expected_hash_branch_reads_both_roots(self, hash_weight, expected):
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {
                    "enabled": True,
                    "expected_state_hash": _MATCHING_HASH,
                    "weight": hash_weight,
                },
            }
        )
        assert grade.components.state_checks == pytest.approx(expected)
        assert "Path not found" not in grade.reasons
        assert "hash matches" in grade.reasons

    def test_no_hash_branch_reads_the_wrapped_state(self):
        grade = _grade({"jsonpaths": _HALF_SATISFIED_JSONPATHS})
        assert grade.components.state_checks == pytest.approx(0.5)
        assert "Path not found" not in grade.reasons


class TestTauStylePackScoresItsHashVerdict:
    """A hash-on pack with no assertions scores the hash verdict itself.

    An empty assertion list declares nothing to evaluate, so it produces no verdict
    for the fold to blend and the hash verdict passes through untouched — otherwise
    the pack would collect jsonpath credit for assertions its author never wrote.
    Pinned across the whole weight domain because that invariance *is* the tau-bench
    parity argument.
    """

    @pytest.mark.parametrize("hash_weight", [0.0, 0.25, 0.5, 0.75, 1.0, None])
    @pytest.mark.parametrize(
        ("expected_state_hash", "expected"),
        [(_MATCHING_HASH, 1.0), (_MISMATCHING_HASH, 0.0)],
    )
    def test_empty_jsonpaths_contribute_nothing(self, hash_weight, expected_state_hash, expected):
        hash_config = {"enabled": True, "expected_state_hash": expected_state_hash}
        if hash_weight is not None:
            hash_config["weight"] = hash_weight

        grade = _grade({"jsonpaths": [], "hash": hash_config})
        assert grade.components.state_checks == pytest.approx(expected)


class TestHashWeightIsRequiredAndBounded:
    """A config that needs a weight and carries none is rejected at load, and an
    out-of-range weight is rejected instead of producing a score outside [0, 1].

    Load time, not grade time: the whole point is that the author hears about it
    before a token is spent on the trial.
    """

    def test_both_sources_without_a_weight_raise_the_shared_message(self):
        with pytest.raises(ValidationError) as excinfo:
            StateChecksConfig(
                jsonpaths=_HALF_SATISFIED_JSONPATHS,
                hash={"enabled": True, "expected_state_hash": _MATCHING_HASH},
            )
        assert MISSING_HASH_WEIGHT_MESSAGE in str(excinfo.value)

    def test_the_engine_never_sees_a_config_it_would_have_to_reject(self):
        with pytest.raises(ValidationError) as excinfo:
            _engine(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": True, "expected_state_hash": _MATCHING_HASH},
                }
            )
        assert MISSING_HASH_WEIGHT_MESSAGE in str(excinfo.value)

    @pytest.mark.parametrize("weight", [2.0, -0.1])
    @pytest.mark.parametrize("jsonpaths", [_HALF_SATISFIED_JSONPATHS, []])
    def test_out_of_range_weight_is_rejected_declared_or_inert(self, weight, jsonpaths):
        with pytest.raises(ValidationError, match="state_checks.hash.weight"):
            StateChecksConfig(
                jsonpaths=jsonpaths,
                hash={
                    "enabled": True,
                    "expected_state_hash": _MISMATCHING_HASH,
                    "weight": weight,
                },
            )

    @pytest.mark.parametrize("declared", [True, "0.5", 2.0, -0.1])
    def test_neither_substrate_accepts_a_non_weight(self, declared):
        """Driven through both config models, because coercion hides two of these.

        Core reads its ``hash`` block as an untyped dict, so its validator sees what
        the author wrote. The runner declares ``hash_weight`` as a typed field, and
        Pydantic's lax coercion turns ``True`` into ``1.0`` and ``"0.5"`` into ``0.5``
        before any after-validator or ``ge``/``le`` bound could object — so
        ``hash_weight: true`` on the wire would silently mean "the hash decides
        outright" on an ``extra="forbid"`` model whose job is rejecting malformed
        input. Testing the shared validator alone would not have seen it.
        """
        with pytest.raises(ValidationError, match="state_checks.hash.weight"):
            StateChecksConfig(
                hash={
                    "enabled": True,
                    "expected_state_hash": _MATCHING_HASH,
                    "weight": declared,
                }
            )
        with pytest.raises(ValidationError, match="state_checks.hash.weight"):
            RunnerStateChecksConfig(
                hash_enabled=True, expected_hash=_MATCHING_HASH, hash_weight=declared
            )

    @pytest.mark.parametrize(("declared", "expected"), [(0.0, 0.0), (0.5, 0.5), (1, 1.0)])
    def test_both_substrates_accept_a_real_weight(self, declared, expected):
        core = StateChecksConfig(
            hash={"enabled": True, "expected_state_hash": _MATCHING_HASH, "weight": declared}
        )
        runner = RunnerStateChecksConfig(
            hash_enabled=True, expected_hash=_MATCHING_HASH, hash_weight=declared
        )

        assert core.hash["weight"] == declared
        assert isinstance(runner.hash_weight, float)
        assert runner.hash_weight == pytest.approx(expected)


class TestLoadTimePredicateDiscriminates:
    """Which author-visible shapes the load-time gate rejects, and which it must not.

    Every accepted row exists to kill a specific over-broad reading of the
    predicate: an unsourced hash produces no verdict to weigh, a disabled hash
    produces none either, and an empty assertion list leaves nothing for a weight
    to divide. Rejecting any of them would demand a number the composer never reads.
    """

    _GOLDEN = [{"name": "close_widget"}]

    @pytest.mark.parametrize(
        ("case", "state_checks", "rejected"),
        [
            (
                "golden_actions and assertions, no weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": True, "golden_actions": _GOLDEN},
                },
                True,
            ),
            (
                "expected_state_hash and assertions, no weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": True, "expected_state_hash": _MATCHING_HASH},
                },
                True,
            ),
            (
                "golden_actions and assertions, with a weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": True, "golden_actions": _GOLDEN, "weight": 0.6},
                },
                False,
            ),
            (
                "expected_state_hash and assertions, with a weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": True,
                        "expected_state_hash": _MATCHING_HASH,
                        "weight": 0.6,
                    },
                },
                False,
            ),
            (
                "hash on with no source, assertions, no weight",
                {"jsonpaths": _HALF_SATISFIED_JSONPATHS, "hash": {"enabled": True}},
                False,
            ),
            (
                "hash off with a source and assertions, no weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": False, "expected_state_hash": _MATCHING_HASH},
                },
                False,
            ),
            (
                "hash off with an inert weight",
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": False,
                        "expected_state_hash": _MATCHING_HASH,
                        "weight": 0.6,
                    },
                },
                False,
            ),
            (
                "golden_actions with no assertions, no weight",
                {"jsonpaths": [], "hash": {"enabled": True, "golden_actions": _GOLDEN}},
                False,
            ),
            (
                "a recorded tau bundle: a weight beside an empty assertion list",
                {
                    "jsonpaths": [],
                    "hash": {
                        "enabled": True,
                        "expected_state_hash": _MATCHING_HASH,
                        "weight": 1.0,
                    },
                },
                False,
            ),
        ],
    )
    def test_shape(self, case, state_checks, rejected):
        if not rejected:
            assert StateChecksConfig(**state_checks) is not None
            return
        with pytest.raises(ValidationError) as excinfo:
            StateChecksConfig(**state_checks)
        assert MISSING_HASH_WEIGHT_MESSAGE in str(excinfo.value)


class TestInertWeightIsReportedNotDropped:
    """A weight that loaded but was never consulted says so on the grade.

    "Accepted and reported" rather than "accepted and ignored" — the recorded tau
    bundles carry a weight beside an empty assertion list, so rejecting one would
    make them unloadable, and dropping it in silence is the disease this milestone
    is about.
    """

    def test_a_tau_shaped_pack_reports_its_unconsulted_weight(self):
        grade = _grade(
            {
                "jsonpaths": [],
                "hash": {"enabled": True, "expected_state_hash": _MATCHING_HASH, "weight": 1.0},
            }
        )
        assert INERT_HASH_WEIGHT_REASON in grade.reasons
        assert grade.components.state_checks == pytest.approx(1.0)

    def test_a_disabled_hash_reports_its_unconsulted_weight(self):
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {
                    "enabled": False,
                    "expected_state_hash": _MATCHING_HASH,
                    "weight": 0.6,
                },
            }
        )
        assert INERT_HASH_WEIGHT_REASON in grade.reasons
        assert grade.components.state_checks == pytest.approx(0.5)

    def test_a_consulted_weight_earns_no_reason(self):
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {"enabled": True, "expected_state_hash": _MATCHING_HASH, "weight": 0.6},
            }
        )
        assert INERT_HASH_WEIGHT_REASON not in grade.reasons
        assert grade.components.state_checks == pytest.approx(0.8)


class TestUnevaluatedHashIsReported:
    """A hash block declaring no source at all says so on the grade rather than
    falling through to jsonpath-only grading in silence.

    The shape is unauthorable — the pre-run gate refuses it — so what grades this way
    is a config mutated after validation or a bundle recorded before the rule.
    """

    def test_hash_enabled_with_no_source_names_the_skipped_check(self):
        grade = _grade({"jsonpaths": _HALF_SATISFIED_JSONPATHS, "hash": {"enabled": True}})
        assert "neither expected_state_hash nor golden_actions" in grade.reasons
        assert "state hash was not checked" in grade.reasons
        assert grade.components.state_checks == pytest.approx(0.5)


class TestAGoldenReplayWithNoWorldRefusesToGrade:
    """Golden actions the engine holds no world for leave the trial ungraded.

    Every case below carries the assertions the trial half-satisfies, so an
    implementation that fell through to them would return ``0.5`` on ``state_checks``
    for a pack whose primary state source never ran — the same number a trial that
    matched the hash on half its assertions earns, which is the defect.
    """

    _UNREPLAYABLE_HASH = {
        "enabled": True,
        "golden_actions": [{"name": "close_widget"}],
        "weight": 0.6,
    }
    #: A world in which nothing is absent, so each case below removes exactly one term
    #: and the message it draws names that term alone.
    _COMPLETE_WORLD = {
        "task_dir": Path("."),
        "task_initial_state": InitialStateConfig(json_db="initial_state.json"),
        "task_mcp_server": "mcp_server.py",
    }
    _TASK_DIR = "no task directory"
    _INITIAL_STATE = "initial_state.json_db"
    _MCP_SERVER = "tools.agent.mcp_server"

    @pytest.mark.parametrize(
        ("case", "world", "named", "unnamed"),
        [
            (
                "no task directory — the caller's omission, not any author's",
                {"task_dir": None},
                [_TASK_DIR],
                [_INITIAL_STATE, _MCP_SERVER],
            ),
            (
                "no initial_state block at all",
                {"task_initial_state": None},
                [_INITIAL_STATE],
                [_TASK_DIR, _MCP_SERVER],
            ),
            (
                "an initial_state block declaring no json_db",
                {"task_initial_state": InitialStateConfig()},
                [_INITIAL_STATE],
                [_TASK_DIR, _MCP_SERVER],
            ),
            (
                "a json_db written inline, where the replay loads a file",
                {"task_initial_state": InitialStateConfig(json_db={"widgets": []})},
                [_INITIAL_STATE, "inline"],
                [_TASK_DIR, _MCP_SERVER],
            ),
            (
                "no mcp_server — the only term production can actually lack",
                {"task_mcp_server": None},
                [_MCP_SERVER],
                [_TASK_DIR, _INITIAL_STATE],
            ),
            (
                "nothing at all, named in one raise rather than one per grading pass",
                {"task_dir": None, "task_initial_state": None, "task_mcp_server": None},
                [_TASK_DIR, _INITIAL_STATE, _MCP_SERVER],
                [],
            ),
        ],
    )
    def test_every_absent_term_is_named_and_no_present_one_is(self, case, world, named, unnamed):
        with pytest.raises(GoldenReplayError) as excinfo:
            _grade(
                {"jsonpaths": _HALF_SATISFIED_JSONPATHS, "hash": self._UNREPLAYABLE_HASH},
                **{**self._COMPLETE_WORLD, **world},
            )

        message = str(excinfo.value)
        assert "GOLDEN REPLAY ERRORS" not in message
        for term in named:
            assert term in message, message
        for term in unnamed:
            assert term not in message, message

    def test_a_complete_world_over_the_real_pack_reaches_a_verdict(self, test_data_dir):
        """The negative control: the hash lands in the component rather than raising.

        ``0.2`` is the blend of a mismatching hash at ``0.6`` with the half-satisfied
        assertions — the ``0.5`` every case above would have produced is what the
        assertions alone are worth, so this number is what proves the predicate did not
        fire and the replay actually happened.
        """
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {
                    "enabled": True,
                    "golden_actions": _GOLDEN_ACTION_THAT_RUNS_WHOLE,
                    "weight": 0.6,
                },
            },
            task_dir=test_data_dir / "tasks" / "shop_orders_02",
            task_initial_state=InitialStateConfig(json_db="initial_state.json"),
            task_mcp_server="mcp_server.py",
        )

        assert grade.components.state_checks == pytest.approx(0.2)

    def test_a_declared_expected_state_hash_needs_no_world(self):
        """The ``grading_parity/all_keys`` shape, graded with an empty world.

        Core returns the in-process comparison against the author's literal before it
        reads ``golden_actions`` at all, so this pack needs nothing to replay against
        and ``0.8`` is the blend of that matching hash with the assertions. A predicate
        hoisted above that early return would refuse a pack core grades correctly.
        """
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {
                    "enabled": True,
                    "expected_state_hash": _MATCHING_HASH,
                    "golden_actions": self._UNREPLAYABLE_HASH["golden_actions"],
                    "weight": 0.6,
                },
            }
        )

        assert grade.components.state_checks == pytest.approx(0.8)
        assert "hash matches" in grade.reasons

    @pytest.mark.parametrize(
        ("case", "task_mcp_server", "suite_ran"),
        [
            ("a world that builds, so the suite is reached", "mcp_server.py", True),
            ("a world that does not, so nothing is", None, False),
        ],
    )
    def test_the_refusal_precedes_every_evaluator(
        self, case, task_mcp_server, suite_ran, tmp_path, test_data_dir
    ):
        """No pack Python runs for a grade that will not exist.

        ``custom_checks`` is the last component ``grade_trajectory`` reaches, and its
        suite is the only component that can run arbitrary pack code, so a sentinel it
        writes is what distinguishes "refused before anything ran" from "refused after".
        The complete-world row is what makes the sentinel's absence mean something.
        """
        pack = tmp_path / "pack"
        shutil.copytree(test_data_dir / "tasks" / "shop_orders_02", pack)
        (pack / "checks.py").write_text(_RECORDING_CHECKS_PY)
        engine = GradingEngine(
            grading_config=GradingConfig(
                state_checks={
                    "jsonpaths": [],
                    "hash": {"enabled": True, "golden_actions": _GOLDEN_ACTION_THAT_RUNS_WHOLE},
                },
                custom_checks={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
                combine={"weights": {"state_checks": 0.5, "custom_checks": 0.5}},
            ),
            task_dir=pack,
            task_initial_state=InitialStateConfig(json_db="initial_state.json"),
            task_mcp_server=task_mcp_server,
        )

        if suite_ran:
            engine.grade_trajectory(_trajectory(), _FINAL_ENV_STATE)
        else:
            with pytest.raises(GoldenReplayError):
                engine.grade_trajectory(_trajectory(), _FINAL_ENV_STATE)

        assert (pack / _CUSTOM_CHECKS_SENTINEL).exists() is suite_ran


class TestFailedGoldenReplayIsNotAScore:
    """A replay that began and failed part-way through produces no verdict either.

    The world below is declared whole — a task directory, a ``json_db`` path and a server
    module — so the refusal that precedes the replay does not fire and what raises is the
    execution itself: the initial-state file the task names is not on disk. The pack
    carries live assertions and a weight, so any implementation that turned the failure
    into an absent hash score would return the full, unweighted jsonpath score (``0.5``) —
    a pass-shaped number for an infrastructure failure.
    """

    def test_replay_failure_raises_instead_of_scoring(self, tmp_path):
        with pytest.raises(GoldenReplayError, match="golden actions"):
            _grade(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": True,
                        "golden_actions": [{"name": "close_widget"}],
                        "weight": 0.6,
                    },
                },
                task_dir=tmp_path,
                task_initial_state=InitialStateConfig(json_db="absent_initial_state.json"),
                task_mcp_server="mcp_server.py",
            )


class TestAnIncompleteReplayIsNamedOnTheGrade:
    """A replay whose action raised still produces a verdict, and says so beside it.

    Driven over the real ``shop_orders_02`` pack because what the sentence counts is how
    many of the pack's own actions ran. The one authored action here resolves and then
    raises on a kwarg its tool does not declare, so the replay leaves the initial state
    untouched — which is what the trial holds too, making the verdict a ``1.0`` for a
    trial that did nothing. The score is asserted because it is the point: the sentence
    is what stands between a reader and trusting it (#816).
    """

    #: A kwarg ``confirm_payment`` does not declare, which is the one defect shape that
    #: makes the tool call itself raise rather than return ``{"error": …}`` (#831).
    _RAISES = [{"name": "confirm_payment", "kwargs": {"order_idd": "O-001"}}]
    #: The read-only action, which runs whole and leaves the initial state alone — the
    #: same verdict as above, arrived at honestly.
    _RUNS_WHOLE = _GOLDEN_ACTION_THAT_RUNS_WHOLE

    def _grade_over_the_pack(self, pack: Path, golden_actions: list[dict]):
        return _grade(
            {"jsonpaths": [], "hash": {"enabled": True, "golden_actions": golden_actions}},
            final_env_state={"db": json.loads((pack / "initial_state.json").read_text())},
            task_dir=pack,
            task_initial_state=InitialStateConfig(json_db="initial_state.json"),
            task_mcp_server="mcp_server.py",
        )

    def test_the_verdict_stands_and_the_reason_names_what_did_not_run(self, test_data_dir):
        grade = self._grade_over_the_pack(test_data_dir / "tasks" / "shop_orders_02", self._RAISES)

        assert grade.components.state_checks == pytest.approx(1.0)
        assert "GOLDEN REPLAY ERRORS:" in grade.reasons
        assert "1 of 1" in grade.reasons
        assert "confirm_payment" in grade.reasons

    def test_a_replay_that_ran_whole_says_nothing(self, test_data_dir):
        """The negative control, same pack and same verdict: the sentence is earned."""
        grade = self._grade_over_the_pack(
            test_data_dir / "tasks" / "shop_orders_02", self._RUNS_WHOLE
        )

        assert grade.components.state_checks == pytest.approx(1.0)
        assert "GOLDEN REPLAY ERRORS" not in grade.reasons


_RUNNER_WIDGETS = {"widgets": [{"id": "W1", "status": "closed"}, {"id": "W2", "status": "open"}]}


def _runner_task_description(state_checks: dict) -> dict:
    """A registrable ``TaskDescription`` graded only by ``state_checks``."""
    return {
        "task_id": "runner_state_checks_fold",
        "name": "Runner state_checks fold",
        "category": "test",
        "description": "A hash source and live assertions, folded by an author weight",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": _RUNNER_WIDGETS,
            "schemas": [{"table_name": "widgets", "fields": {"id": "string", "status": "string"}}],
            "unstable_fields": [],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
            "state_checks": {"jsonpath_checks": _HALF_SATISFIED_JSONPATHS, **state_checks},
        },
    }


def _spec_payload(trial_id: str, state_checks: dict) -> dict:
    return json.loads(trial_spec_json(_runner_task_description(state_checks), trial_id=trial_id))


def _register(runner_service, mock_grpc_context, trial_id: str, spec_payload: dict):
    request = register_request(json.dumps(spec_payload), trial_id=trial_id)
    return runner_service.RegisterTrial(request, mock_grpc_context)


class TestRunnerRejectsTheUndecidableConfig:
    """``RegisterTrial`` rejects the shapes the core config rejects, via one predicate.

    The weight is stripped from the serialised spec rather than left out of the
    model, because that is the only way the shape reaches a runner in production: an
    engine predating the field emits a ``state_checks`` block without it, and the
    trial spec crosses the wire as a plain ``model_dump_json()``. The assertion is on
    the *message*, so pointing the runner at its own copy of the predicate reddens it.
    """

    # Author-facing hash-source key -> the runner fields the adapter flattens it onto.
    _SOURCES = {
        "expected_state_hash": {"expected_hash": _MATCHING_HASH},
        "golden_actions": {"golden_actions": [{"tool_name": "close_widget", "arguments": {}}]},
    }

    def test_every_hash_source_is_driven_through_registration(self):
        """A source the runner model does not flatten would slip past its gate.

        The runner rebuilds the author-facing ``hash`` block from its own flattened
        fields before calling the shared predicate, so a hash source added to
        ``HASH_SOURCE_KEYS`` and not to that translation leaves the gate blind to it —
        the trial registers and then fails at ``GradeTrial`` instead. Holding this map
        to the exported vocabulary is what forces the pair of edits together.
        """
        assert set(self._SOURCES) == set(HASH_SOURCE_KEYS)

    def _weighted(self, source_key: str) -> dict:
        return {"hash_enabled": True, "hash_weight": 0.6, **self._SOURCES[source_key]}

    @pytest.mark.parametrize("source_key", sorted(_SOURCES))
    def test_a_stripped_weight_is_rejected(self, source_key, runner_service, mock_grpc_context):
        trial_id = f"gate_stripped_{source_key}:0"
        payload = _spec_payload(trial_id, self._weighted(source_key))
        del payload["task"]["grading"]["state_checks"]["hash_weight"]

        response = _register(runner_service, mock_grpc_context, trial_id, payload)

        assert response.success is False
        assert MISSING_HASH_WEIGHT_MESSAGE in response.error

    @pytest.mark.parametrize("source_key", sorted(_SOURCES))
    def test_the_same_spec_registers_with_its_weight(
        self, source_key, runner_service, mock_grpc_context
    ):
        trial_id = f"gate_declared_{source_key}:0"

        response = _register(
            runner_service,
            mock_grpc_context,
            trial_id,
            _spec_payload(trial_id, self._weighted(source_key)),
        )

        assert response.success is True, response.error


class TestGradeTrialFoldsByTheAuthorWeight:
    """``GradeTrial`` folds a live hash verdict with a live assertion score.

    Both sources are real here: hash grading runs with empty ``golden_actions``
    (expected state == initial state) against a trial that mutated nothing, so its
    verdict is a genuine ``1.0``, and the fixture's two assertions leave the JSONPath
    score at ``0.5``. The product rule would return ``0.5`` at every weight.

    This shape is also the one ``RegisterTrial`` accepts and the runner cannot always
    fold: ``hash.enabled`` with no *declared* source is not undecidable at load —
    core produces no verdict for it — but the runner's refusal semantics produce one
    anyway. Without a weight the component is undecidable at grade time, and the RPC
    says so rather than returning a grade folded by an invented rule. It arrives here
    as a wire payload because no pack can declare it: the authoring gate refuses the
    flag with no source, so what reaches this fold is a directly built description or
    a bundle recorded before that rule.
    """

    _REFUSAL_HASH = {"hash_enabled": True, "golden_actions": []}

    @pytest.mark.parametrize(("hash_weight", "expected"), [(0.6, 0.8), (0.25, 0.625), (0.0, 0.5)])
    def test_component_is_the_blend(self, hash_weight, expected, runner_service, mock_grpc_context):
        trial_id = f"fold_{hash_weight}:0"
        state_checks = {**self._REFUSAL_HASH, "hash_weight": hash_weight}
        registered = _register(
            runner_service, mock_grpc_context, trial_id, _spec_payload(trial_id, state_checks)
        )
        assert registered.success is True, registered.error

        response = runner_service.GradeTrial(
            pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is True, response.error
        assert response.grade.components.state_checks == pytest.approx(expected)
        assert INERT_HASH_WEIGHT_REASON not in response.grade.reasons

    def test_a_weight_the_fold_skipped_is_reported_on_the_grade(
        self, runner_service, mock_grpc_context
    ):
        """The tau shape: a hash verdict, no assertions, and a weight nothing divides.

        The runner grades every production trial, so a weight it silently skipped is
        the "accepted and ignored" shape this milestone exists to end. Core reports it
        from the same constant.
        """
        trial_id = "fold_inert:0"
        state_checks = {**self._REFUSAL_HASH, "hash_weight": 1.0, "jsonpath_checks": []}
        registered = _register(
            runner_service, mock_grpc_context, trial_id, _spec_payload(trial_id, state_checks)
        )
        assert registered.success is True, registered.error

        response = runner_service.GradeTrial(
            pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is True, response.error
        assert response.grade.components.state_checks == pytest.approx(1.0)
        assert INERT_HASH_WEIGHT_REASON in response.grade.reasons

    def test_an_undecidable_fold_fails_the_rpc_naming_the_trial(
        self, runner_service, mock_grpc_context
    ):
        trial_id = "fold_undecidable:0"
        registered = _register(
            runner_service, mock_grpc_context, trial_id, _spec_payload(trial_id, self._REFUSAL_HASH)
        )
        assert registered.success is True, registered.error

        response = runner_service.GradeTrial(
            pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is False
        assert not response.HasField("grade")
        assert trial_id in response.error
        assert MISSING_HASH_WEIGHT_MESSAGE in response.error


class TestWeightCarryingPacksReachTheRunner:
    """The two in-repo packs that declare a weight arrive at the runner carrying it.

    Both configure a hash source *and* live assertions, so a gate that rejected the
    shape, or a translation that dropped the value, would take them out of service
    entirely.
    """

    @pytest.mark.parametrize(
        ("tasks_glob", "task_id", "weight"),
        [
            ("tasks/**/task.yaml", "shop_orders_02", 0.6),
            ("grading_parity/**/task.yaml", "all_keys", 0.75),
        ],
    )
    def test_translated_weight(self, tasks_glob, task_id, weight, test_data_dir):
        adapter = NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": tasks_glob})

        state_checks = adapter.to_task_description(task_id).grading.state_checks

        assert state_checks.hash_weight == pytest.approx(weight)
