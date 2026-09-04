"""What a golden replay does with an authored action that did not take effect.

Three shapes, and the line between them is what the replay can still produce. A name
resolving to nothing is refused before anything runs, because no world can be built from
it. An action that resolved and *raised*, and one that resolved, ran and *reported* its
failure in what it returned, have each left a world behind — a partial one — so the hash
against it is still computed and the grade names the action missing from it.

Both substrates, because both replay golden actions and each resolves a name under its
own rule: the core engine against the pack's ``TOOLS`` map exactly, the runner against
the tools it registered for the trial with a single ``…_<name>`` suffix allowed on top.

The core half is driven over the real ``tests/data/tasks/shop_orders_02`` pack — its
``mcp_server.py``, its ``initial_state.json``, and the two-step golden path its
``grading.yaml`` declares. The pack's own ``TOOLS`` map is what decides which names
resolve, so a stand-in map would lock the stand-in's rule rather than the one a replay
actually applies; and the trial states below are produced by driving the pack's tools,
which is how an agent produces them. ``food_delivery_2`` joins it for the payload shape
that pack cannot produce: its tools return the JSON *of* their answer, so the replay is
handed a ``str`` where ``shop_orders_02`` hands it a mapping.

The measured defects these cases close, both on a trial that placed the order and never
paid — the wrong behaviour the pack exists to catch. With ``confirm_payment`` misspelled,
the replay skipped it and returned the order still ``pending``, so the wrong trial hashed
equal to that partial world and scored ``1.0`` "State hash matches", while a correct trial
scored ``0.0`` against a diff that named nothing about the typo. With its kwargs written
``{"order_id": "O-999"}``, the tool declined the call and said so in its return value, the
replay recorded nothing at all, and the same wrong trial scored ``1.0`` beside a
completely clean grade. A runner that resolved a name inside its replay loop rather than
ahead of it scores the trial against the same partial world and annotates the grade
instead of refusing it; the runner cases below rule that out.

The runner half is driven through its own ``_execute_hash_grading`` over a db-client that
answers the hash path, because what reaches it is not the mapping core reads but the text
its wrapper flattened one out of: measured over the real MCP subprocess, the same declined
call arrives as ``'{\\n  "error": "Order \\'O-999\\' not found"\\n}'``, and every payload it
reads — success and failure alike — is a ``str``. A raise reaches it as a ``str`` there too,
that substrate flattening the exception into prose and stating the failure out of band in
the flag beside it, which the wrapper reports and the loop records as the raised kind.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.utils.golden_source_shapes import elements_that_are_no_action
from tolokaforge.core.grading.composite_fold import build_grade_reasons
from tolokaforge.core.grading.golden_replay import (
    FailedGoldenAction,
    GoldenActionFailure,
    GoldenReplayRecord,
    UnresolvableGoldenAction,
    declared_failure,
    incomplete_replay_reason,
    resolve_golden_action_names,
)
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.runner.models import (
    GoldenAction,
    HashComparisonBasis,
    HashGradingResult,
    RunnerStateChecksConfig,
    TaskDescription,
)
from tolokaforge.runner.service import (
    RunnerServiceImpl,
    TrialContextRuntime,
    _tool_registered_for_trial,
)
from tolokaforge.runner.tool_factory import ToolCallOutcome

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[2]
_TASK_DIR = _REPO / "tests/data/tasks/shop_orders_02"
_INITIAL_STATE = "initial_state.json"
_MCP_SERVER = "mcp_server.py"

#: Every name the pack's MCP module exposes, which is the set a golden action name is
#: resolved against and therefore what an author of an unresolvable one has to be told.
_PACK_TOOLS = ("confirm_payment", "get_customer", "list_products", "place_order")

_PLACE_ORDER = (
    "place_order",
    {
        "customer_id": "C-101",
        "items": [
            {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
            {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
        ],
    },
)
_CONFIRM_PAYMENT = ("confirm_payment", {"order_id": "O-001"})


@pytest.fixture(scope="module")
def pack_tools() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("_shop_orders_02", _TASK_DIR / _MCP_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TOOLS


@pytest.fixture(scope="module")
def golden_actions() -> list[dict[str, Any]]:
    """The pack's authored golden path, read from the pack rather than restated here."""
    grading = yaml.safe_load((_TASK_DIR / "grading.yaml").read_text())
    return grading["state_checks"]["hash"]["golden_actions"]


def _misspell_payment(authored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = copy.deepcopy(authored)
    actions[1]["name"] = "confirm_paymnet"
    return actions


def _misspell_payment_kwarg(authored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pack's second action, defective in the one way that makes ``invoke`` raise.

    A kwarg the tool's signature does not declare fails the ``func(data, **kwargs)`` call
    itself, outside the registry wrapper's ``except ToolError`` arm. A *declared* failure
    goes through that arm instead and raises nothing, which is
    :func:`_report_payment_failure` below.
    """
    actions = copy.deepcopy(authored)
    actions[1]["kwargs"] = {"order_idd": "O-001"}
    return actions


def _report_payment_failure(authored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pack's second action, defective in the way the tool itself declares.

    ``O-999`` is no order in the pack's initial state, so ``confirm_payment`` raises
    ``ToolError`` from its body and the registry converts that into a returned
    ``{"error": …}`` payload — which is how *every* declared failure in a ``create_server``
    pack reaches the replay, the raise being the registry's to catch rather than the
    replay's.
    """
    actions = copy.deepcopy(authored)
    actions[1]["kwargs"] = {"order_id": "O-999"}
    return actions


def _trial_state(pack_tools: dict[str, Any], *calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """The database an agent that made exactly these calls leaves behind."""
    data = json.loads((_TASK_DIR / _INITIAL_STATE).read_text())
    for name, kwargs in calls:
        pack_tools[name].invoke(data=data, **kwargs)
    return data


def _check(
    db_state: dict[str, Any], actions: list[dict[str, Any]]
) -> tuple[float, str, dict[str, Any] | None, GoldenReplayRecord]:
    return StateChecker().check_hash_against_golden_replay(
        db_state=db_state,
        golden_actions=actions,
        task_dir=_TASK_DIR,
        initial_state_path=_INITIAL_STATE,
        mcp_server_path=_MCP_SERVER,
        task_domain="shop",
    )


def test_a_misspelled_action_raises_where_it_used_to_pass_the_wrong_trial(
    pack_tools, golden_actions
) -> None:
    """The measured wrong verdict, asserted gone, and the sentence that replaces it.

    The trial placed the order and never paid, which is the failure this pack exists to
    catch, and it hashes equal to the world the misspelled golden path replays. Any
    implementation that skipped the unresolvable action would return ``1.0`` "State hash
    matches" here. The message has to carry the name as written and the set it was
    resolved against, because those two are the whole of what tells an author it is the
    golden path that is broken and not the agent.
    """
    never_paid = _trial_state(pack_tools, _PLACE_ORDER)

    with pytest.raises(UnresolvableGoldenAction) as raised:
        _check(never_paid, _misspell_payment(golden_actions))

    message = str(raised.value)
    assert "confirm_paymnet" in message, message
    for name in _PACK_TOOLS:
        assert name in message, message


def test_an_action_declaring_no_name_is_refused_rather_than_skipped(
    pack_tools, golden_actions
) -> None:
    nameless = [copy.deepcopy(golden_actions[0]), {"kwargs": {"order_id": "O-001"}}]

    with pytest.raises(UnresolvableGoldenAction, match=r"\[1\] None"):
        _check(_trial_state(pack_tools, _PLACE_ORDER), nameless)


def test_an_action_whose_name_is_no_string_is_refused_as_one_naming_nothing(
    pack_tools, golden_actions
) -> None:
    """The ``hash`` block is untyped, so a name may arrive as a list — and must not raise.

    The subclass is the assertion. Reaching the matcher, an unhashable name answers its
    membership test with a ``TypeError``, which the wrapper flattens into the base
    ``GoldenReplayError`` — so the pack defect arrives as "Error executing golden actions"
    rather than as the class whose message names the offending action and the pack's
    tools.
    """
    listed = [copy.deepcopy(golden_actions[0]), {"name": ["confirm_payment"]}]

    with pytest.raises(UnresolvableGoldenAction, match=r"\[1\] None"):
        _check(_trial_state(pack_tools, _PLACE_ORDER), listed)


#: Shared with the runner's read of the same shapes, over this pack's second action.
_ELEMENTS_THAT_ARE_NO_ACTION = elements_that_are_no_action("confirm_payment")


@pytest.mark.parametrize("element", _ELEMENTS_THAT_ARE_NO_ACTION)
def test_an_action_that_is_no_mapping_at_all_is_refused_by_its_index(
    pack_tools, golden_actions, element
) -> None:
    """The class the replay's own ``Raises:`` block promises, over a list of anything.

    ``golden_actions`` claims nothing about its elements (#907), so nothing stops one being
    a bare string where a mapping belongs. Reading a name off it raises an
    ``AttributeError`` the wrapper flattens into the base ``GoldenReplayError`` — "Error
    executing golden actions: 'str' object has no attribute 'get'", which names neither the
    offending index nor a fix. Resolution answers it instead: an element declaring no name
    resolves to nothing, exactly as an action carrying no ``name`` key does, and the two
    take the same fix.
    """
    listed = [copy.deepcopy(golden_actions[0]), element]

    with pytest.raises(UnresolvableGoldenAction, match=r"\[1\] None"):
        _check(_trial_state(pack_tools, _PLACE_ORDER), listed)


def test_every_unresolvable_action_is_named_in_one_raise(pack_tools, golden_actions) -> None:
    """Two defects, one exception — an author fixing a golden path sees the whole list."""
    both_misspelled = _misspell_payment(golden_actions)
    both_misspelled[0]["name"] = "place_ordr"

    with pytest.raises(UnresolvableGoldenAction) as raised:
        _check(_trial_state(pack_tools, _PLACE_ORDER), both_misspelled)

    assert "place_ordr" in str(raised.value)
    assert "confirm_paymnet" in str(raised.value)


def test_the_pack_as_authored_still_grades_a_correct_trial_as_a_pass(
    pack_tools, golden_actions
) -> None:
    """The negative control: resolving before executing moves no correct pack's verdict.

    A replay that ran whole earns no sentence either — the state hashed against is the
    world the pack describes, so there is nothing to report about it.
    """
    paid = _trial_state(pack_tools, _PLACE_ORDER, _CONFIRM_PAYMENT)

    score, reason, diff, replay = _check(paid, copy.deepcopy(golden_actions))

    assert (score, diff) == (1.0, None)
    assert reason == "State hash matches"
    assert incomplete_replay_reason(replay) is None


def test_the_pack_as_authored_still_fails_a_trial_that_never_paid(
    pack_tools, golden_actions
) -> None:
    never_paid = _trial_state(pack_tools, _PLACE_ORDER)

    score, reason, diff, _ = _check(never_paid, copy.deepcopy(golden_actions))

    assert score == 0.0
    assert "State hash mismatch" in reason
    assert diff is not None


def test_an_action_that_raised_still_scores_the_trial_and_says_what_did_not_take_effect(
    pack_tools, golden_actions
) -> None:
    """The partial world is still hashed against, and the grade names what is missing.

    The verdict here is the wrong one and asserted anyway: the trial placed the order and
    never paid, and because the golden path's ``confirm_payment`` raised, the world it
    replayed stops at the same place, so a trial that failed the task hashes equal to the
    oracle and scores ``1.0``. Whether such a verdict should exist at all is #816 — this
    case pins that it does, and that a reader of the grade is told why not to trust it.
    """
    never_paid = _trial_state(pack_tools, _PLACE_ORDER)

    score, reason, _, replay = _check(never_paid, _misspell_payment_kwarg(golden_actions))

    assert score == 1.0
    assert reason == "State hash matches"

    sentence = incomplete_replay_reason(replay)
    assert sentence is not None
    assert sentence.startswith("GOLDEN REPLAY ERRORS:"), sentence
    assert "1 of 2" in sentence, sentence
    assert "confirm_payment raised TypeError" in sentence, sentence
    assert "order_idd" in sentence, sentence


def test_an_action_that_reported_its_failure_is_recorded_like_one_that_raised(
    pack_tools, golden_actions
) -> None:
    """The same wrong ``1.0``, now annotated — the action ran and declined the request.

    Nothing raises here: the pack's own tool answered ``{"error": …}`` and the replay
    walked on, leaving the order ``pending``, which is exactly where the trial left it. So
    the trial that failed the task hashed equal to the oracle and collected a *clean*
    grade. The score is asserted because it is the point — #816 owns whether such a verdict
    should exist, and this pins that until then a reader is at least told about it — and the
    verb is asserted because it is what sends the author to the right defect: ``raised``
    means the golden path calls the tool wrong, ``reported`` means it calls it right about a
    state that refuses it.
    """
    never_paid = _trial_state(pack_tools, _PLACE_ORDER)

    score, reason, _, replay = _check(never_paid, _report_payment_failure(golden_actions))

    assert score == 1.0
    assert reason == "State hash matches"

    sentence = incomplete_replay_reason(replay)
    assert sentence is not None
    assert sentence.startswith("GOLDEN REPLAY ERRORS:"), sentence
    assert "1 of 2" in sentence, sentence
    assert "[1] confirm_payment reported Order 'O-999' not found" in sentence, sentence


def test_an_action_whose_tool_returns_a_list_reports_no_failure(pack_tools, golden_actions) -> None:
    """The pack's real non-mapping return, which carries no ``"error"`` to read at all.

    ``list_products`` answers with a ``list``, so a predicate reading a top-level key has
    nothing to read — and appending it to the authored path must therefore leave both the
    record and the verdict exactly as the authored path leaves them.
    """
    paid = _trial_state(pack_tools, _PLACE_ORDER, _CONFIRM_PAYMENT)
    listing_too = copy.deepcopy(golden_actions) + [{"name": "list_products"}]

    score, reason, _, replay = _check(paid, listing_too)

    assert (score, reason) == (1.0, "State hash matches")
    assert replay == GoldenReplayRecord(authored=3)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param({"error": "Order 'O-999' not found"}, "Order 'O-999' not found", id="mapping"),
        pytest.param('{"error": "user not found"}', "user not found", id="json-string"),
        pytest.param(
            {"error": "invalid order", "details": ["items is empty", "total is 0"]},
            "invalid order (items is empty; total is 0)",
            id="details-appended",
        ),
        pytest.param(
            {"error": "invalid order", "details": "items is empty"},
            "invalid order",
            id="non-list-details-ignored",
        ),
        pytest.param(
            {"error": "invalid order", "details": []}, "invalid order", id="empty-details"
        ),
        pytest.param({"error": {"code": 5}}, "{'code': 5}", id="non-string-error-coerced"),
        pytest.param({"error": None}, None, id="null-error-is-no-error"),
        pytest.param({"error": ""}, None, id="empty-error-is-no-error"),
        pytest.param({"id": "O-001", "status": "paid"}, None, id="success-mapping"),
        pytest.param('{"user_id": "user_5247"}', None, id="success-json-string"),
        pytest.param({"result": {"error": "nope"}}, None, id="nested-error-is-not-top-level"),
        pytest.param(
            "Error executing tool confirm_payment: 1 validation error",
            None,
            id="prose-is-not-a-payload",
        ),
        pytest.param('{"id": "P-001"}\n{"id": "P-002"}', None, id="concatenated-json-objects"),
        pytest.param('[{"error": "x"}]', None, id="json-string-decoding-to-a-list"),
        pytest.param([{"id": "P-001"}], None, id="list"),
        pytest.param(None, None, id="nothing-returned"),
    ],
)
def test_declared_failure_reads_a_truthy_top_level_error_and_nothing_else(
    payload: object, message: str | None
) -> None:
    """The one predicate both substrates read a returned payload with.

    Two rows are load-bearing beyond the obvious. The prose row is what FastMCP answers a
    *raising* MCP tool with, and it must stay undetected here: matching prose for "error" is
    the shape that false-positives on legitimate tool output (#855), and that call's failure
    reaches the replay loop as the flag its substrate stated beside the text rather than as
    anything this predicate could read out of the text. The concatenated-JSON row is what
    flattening a ``list`` return's per-element text blocks produces, which is no JSON
    document and no failure either.
    """
    assert declared_failure(payload) == message


# ---------------------------------------------------------------------------
# A pack whose tools return the JSON of their answer rather than the answer
# ---------------------------------------------------------------------------

#: ``food_delivery_2`` is tau-style: every tool ``json.dumps`` what it returns, so its
#: whole population reaches the predicate as ``str``. Its own constants rather than the
#: module's, which are ``shop_orders_02``'s.
_TAU_TASK_DIR = _REPO / "tests/data/projects/food_delivery_2/tasks/order_modify_with_checks"
#: Both paths leave the task directory, which is how the pack writes them and how the
#: replay resolves them.
_TAU_INITIAL_STATE = "../../data/combined_initial_state.json"
_TAU_MCP_SERVER = "../../mcp_server.py"


@pytest.fixture(scope="module")
def tau_golden_actions() -> list[dict[str, Any]]:
    """The tau pack's authored golden path, read from the pack rather than restated here."""
    grading = yaml.safe_load((_TAU_TASK_DIR / "grading.yaml").read_text())
    return grading["state_checks"]["hash"]["golden_actions"]


def _replay_the_tau_pack(actions: list[dict[str, Any]]) -> GoldenReplayRecord:
    _, _, _, replay = StateChecker().check_hash_against_golden_replay(
        db_state=json.loads((_TAU_TASK_DIR / _TAU_INITIAL_STATE).read_text()),
        golden_actions=actions,
        task_dir=_TAU_TASK_DIR,
        initial_state_path=_TAU_INITIAL_STATE,
        mcp_server_path=_TAU_MCP_SERVER,
        task_domain="food_delivery",
    )
    return replay


def test_a_tau_pack_records_the_failure_its_tool_returned_as_a_json_string(
    tau_golden_actions,
) -> None:
    """The decoded message, not the JSON, so the sentence reads as the tool wrote it."""
    no_such_user = copy.deepcopy(tau_golden_actions)
    no_such_user[0]["kwargs"] = {"user_id": "NO_SUCH_USER"}

    replay = _replay_the_tau_pack(no_such_user)

    assert replay.failures == (
        FailedGoldenAction(
            index=0,
            name="get_user_details",
            kind=GoldenActionFailure.REPORTED,
            error="user not found",
        ),
    )
    sentence = incomplete_replay_reason(replay)
    assert sentence is not None
    assert "1 of 3" in sentence, sentence
    assert "[0] get_user_details reported user not found" in sentence, sentence


def test_the_tau_pack_as_authored_records_no_failure(tau_golden_actions) -> None:
    """The negative control for the whole ``str`` population.

    Reading a ``str`` admits every tau success payload into the predicate's input, so
    without this a branch that flagged all of them would pass the case above and move the
    grade of every tau pack in the tree.
    """
    replay = _replay_the_tau_pack(copy.deepcopy(tau_golden_actions))

    assert replay == GoldenReplayRecord(authored=3)
    assert incomplete_replay_reason(replay) is None


# ---------------------------------------------------------------------------
# The runner: its own matcher, and the trial state it may not touch
# ---------------------------------------------------------------------------

#: What ``RegisterTrial`` left in ``agent_tools`` — one name a golden action can only
#: reach through the suffix rule, one it reaches exactly.
_REGISTERED_TOOLS = ("shop_place_order", "confirm_payment")


def _resolve_as_the_runner_does(*names: str | None) -> list[str]:
    return resolve_golden_action_names(
        list(names), candidates=_REGISTERED_TOOLS, match=_tool_registered_for_trial
    )


def test_the_runner_resolves_both_an_exact_name_and_an_unprefixed_one() -> None:
    """Golden actions are authored unprefixed, so both spellings have to reach a tool."""
    assert _resolve_as_the_runner_does("confirm_payment", "place_order") == [
        "confirm_payment",
        "shop_place_order",
    ]


def test_the_runner_refuses_a_name_naming_the_index_and_the_registered_set() -> None:
    """An author reading this has to learn which action is wrong and what it could say."""
    with pytest.raises(UnresolvableGoldenAction) as raised:
        _resolve_as_the_runner_does("confirm_payment", "place_ordr")

    message = str(raised.value)
    assert "[1] 'place_ordr'" in message, message
    for name in _REGISTERED_TOOLS:
        assert name in message, message


def _replaying(*golden_actions: GoldenAction) -> RunnerStateChecksConfig:
    """The state-checks config a pack declaring a golden replay hands the evaluator.

    The evaluator selects its comparison basis from the config, so the actions reach
    it inside one — and a replay is the only basis under which it reads them.
    """
    return RunnerStateChecksConfig(hash_enabled=True, golden_actions=list(golden_actions))


class _RefusingDBClient:
    """Every db-service call refused, whichever it is.

    Steps 1-4 of the runner's hash path all write to the trial's database — an MCP
    task syncs through ``mutate``, then the trial is snapshotted and reset to its
    initial state. Refusing the whole client asserts that resolution finishes before
    any of them is reached without restating a call order the implementation is free
    to change.
    """

    def __getattr__(self, method: str) -> Callable[..., Any]:
        async def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(f"db_client.{method} ran before golden actions resolved")

        return refuse


def _runner_over_a_refusing_db() -> tuple[RunnerServiceImpl, TrialContextRuntime]:
    service = RunnerServiceImpl.__new__(RunnerServiceImpl)
    service.db_client = _RefusingDBClient()
    context = TrialContextRuntime(
        trial_id="golden_replay_ordering:0",
        task_description=TaskDescription(
            task_id="golden_replay_ordering",
            name="Golden-action resolution precedes every db call",
            category="test",
            description="A hash-graded trial whose golden action names an unknown tool",
            adapter_type="native",
            system_prompt="You are a test assistant.",
        ),
    )
    context.agent_tools = dict.fromkeys(_REGISTERED_TOOLS, object())
    return service, context


async def test_an_unresolvable_name_fails_the_grade_before_the_trial_state_moves() -> None:
    """The invariant: a pack defect leaves the trial's database exactly as it was.

    The replay loop sits between ``reset_trial`` and ``restore_snapshot``, so a raise
    from inside it would leave the trial holding the initial state instead of what the
    agent left behind. Resolving first is what makes the failure free of that cost.
    """
    service, context = _runner_over_a_refusing_db()

    with pytest.raises(UnresolvableGoldenAction, match="place_ordr"):
        await service._execute_hash_grading(
            context.trial_id, context, _replaying(GoldenAction(tool_name="place_ordr"))
        )


async def test_a_resolvable_name_reaches_the_db_client_the_refusal_guards() -> None:
    """The control: the test above passes because resolution precedes the db calls.

    Without this, a hash path that had stopped touching the database at all — or a
    resolution pass that raised on every name — would satisfy the invariant vacuously.
    """
    service, context = _runner_over_a_refusing_db()

    with pytest.raises(AssertionError, match="ran before golden actions resolved"):
        await service._execute_hash_grading(
            context.trial_id, context, _replaying(GoldenAction(tool_name="place_order"))
        )


class _AnsweringDBClient:
    """The hash path answered rather than refused, so the replay loop is reached at all.

    One hash for the trial and for the golden world, which compares equal — the record the
    loop built is what these cases read, and a mismatch would send the caller on to
    ``get_stable_state`` for a diff no state behind this client can produce.
    """

    async def get_stable_hash(self, _trial_id: str, **_kwargs: Any) -> str:
        return "0" * 64

    async def create_snapshot(self, _trial_id: str, _label: str) -> None:
        return None

    async def reset_trial(self, _trial_id: str) -> None:
        return None

    async def restore_snapshot(self, _trial_id: str, _label: str) -> None:
        return None


class _AnsweringTool:
    """A registered tool answering one payload, reached through ``execute`` as a wrapper is.

    No ``MCPServerToolWrapper``, so ``_find_mcp_server_wrapper`` answers ``None`` and the
    subprocess state syncs stay out of these cases.
    """

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def execute(self, _arguments: dict[str, Any]) -> str:
        return self._answer


class _FlaggingTool:
    """A tool whose substrate declares a failed call in a flag beside the text it answered.

    ``MCPServerToolWrapper``'s shape: the flag reaches the replay loop through
    ``execute_call``, while ``execute`` hands the same text over for the agent path. That
    the protocol really states it, and states it for exactly the calls whose exception
    escaped the tool, is locked against the pack's own subprocess in
    ``tests/canonical/test_shop_orders_02_behavior_canon.py``; what these cases read is what
    the loop does once it holds one. Joining the two halves inside a runner container is
    #860.
    """

    def __init__(self, answer: str, *, declared_failure: bool) -> None:
        self._outcome = ToolCallOutcome(output=answer, declared_failure=declared_failure)

    async def execute(self, _arguments: dict[str, Any]) -> str:
        return self._outcome.output

    async def execute_call(self, _arguments: dict[str, Any]) -> ToolCallOutcome:
        return self._outcome


def _flagging_tool(answer: str) -> _FlaggingTool:
    return _FlaggingTool(answer, declared_failure=True)


def _unflagged_tool(answer: str) -> _FlaggingTool:
    return _FlaggingTool(answer, declared_failure=False)


def _answering_async_callable(answer: str) -> Callable[[dict[str, Any]], Awaitable[str]]:
    async def call(_arguments: dict[str, Any]) -> str:
        return answer

    return call


def _answering_sync_callable(answer: str) -> Callable[[dict[str, Any]], str]:
    def call(_arguments: dict[str, Any]) -> str:
        return answer

    return call


class _ShapelessTool:
    """A registered object the replay loop reaches through no shape at all.

    Neither ``execute_call`` nor ``execute``, and not callable, so no answer exists to read
    the golden action's effect out of.
    """

    def __init__(self, _answer: str) -> None:
        """The answer goes nowhere: no shape on this object can be asked for one."""


class _AsyncCallTool:
    """A callable object whose ``__call__`` is ``async``, so the loop's executor awaits nothing.

    :func:`inspect.iscoroutinefunction` reads the object rather than its ``__call__``, so this
    shape takes the sync-executor arm and the call answers a coroutine.
    """

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def __call__(self, _arguments: dict[str, Any]) -> str:
        return self._answer


class _OutcomelessTool:
    """A tool whose ``execute_call`` answers the text alone rather than an outcome.

    The shape a wrapper over a substrate with its own failure flag is written into: the
    method exists, so the loop takes that arm, and what comes back carries no flag to read.
    """

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def execute_call(self, _arguments: dict[str, Any]) -> str:
        return self._answer


def _answering_mapping_callable(answer: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A sync callable answering the mapping ``answer`` carries rather than its text.

    The shape core is handed by a pack's ``invoke`` and the runner never is, every payload it
    reads being a ``str``: reachable here only from a duck-typed callable in ``agent_tools``.
    """

    def call(_arguments: dict[str, Any]) -> dict[str, Any]:
        return json.loads(answer)

    return call


#: The three fallback shapes the replay loop reaches a registered tool through, each of which
#: has to hand its answer back: a shape whose return were dropped would record nothing any
#: pack signalling through it declared, and the record would read as a whole golden path.
#: ``RegisterTrial`` leaves ``ToolWrapper`` instances behind, which the loop reaches through
#: ``execute_call`` instead — ``_FlaggingTool`` is that shape.
_TOOL_SHAPES = (
    pytest.param(_AnsweringTool, id="execute"),
    pytest.param(_answering_async_callable, id="async-callable"),
    pytest.param(_answering_sync_callable, id="sync-callable"),
)


#: What an ``MCP_SERVER`` pack answers a call its tool declined, measured over the pack's
#: subprocess: FastMCP renders the returned ``{"error": …}`` mapping as one text block of
#: ``json.dumps(payload, indent=2)``, which the wrapper hands over as a ``str``.
_DECLINED_PAYLOAD = '{\n  "error": "Order \'O-999\' not found"\n}'

#: What the same substrate answers ``confirm_payment({'order_idd': 'O-001'})``, a kwarg the
#: tool's signature does not declare so the call itself fails: the exception never reaches
#: the caller, FastMCP flattening it into multi-line prose and stating the failure out of
#: band in ``isError``. Pinned whole, prefix and newlines included, because that is what the
#: record carries and what an author looking for this string in a log will find.
_FLAGGED_PROSE = (
    "Error executing tool confirm_payment: 1 validation error for confirm_paymentArguments\n"
    "order_id\n"
    "  Field required [type=missing, input_value={'order_idd': 'O-001'}, input_type=dict]\n"
    "    For further information visit https://errors.pydantic.dev/2.13/v/missing"
)


async def _replay_as_the_runner_does(
    answer: str, build_tool: Callable[[str], Any] = _AnsweringTool
) -> HashGradingResult:
    service = RunnerServiceImpl.__new__(RunnerServiceImpl)
    service.db_client = _AnsweringDBClient()
    context = TrialContextRuntime(
        trial_id="golden_replay_failed:0",
        task_description=TaskDescription(
            task_id="golden_replay_failed",
            name="A golden action that did not take effect",
            category="test",
            description="A hash-graded trial whose golden action its tool or substrate failed",
            adapter_type="native",
            system_prompt="You are a test assistant.",
        ),
    )
    context.agent_tools = {"confirm_payment": build_tool(answer)}
    return await service._execute_hash_grading(
        context.trial_id,
        context,
        _replaying(GoldenAction(tool_name="confirm_payment", arguments={"order_id": "O-999"})),
    )


def _reasons_for(result: HashGradingResult) -> str:
    return build_grade_reasons(
        {"hash_score": result.hash_score, "hash_match": result.hash_match},
        golden_replay=result.golden_replay,
    )


@pytest.mark.parametrize("build_tool", _TOOL_SHAPES)
async def test_the_runner_records_the_failure_its_tool_reported_in_what_it_returned(
    build_tool: Callable[[str], Any],
) -> None:
    """The runner's half of the same defect, through the payload its wrapper really returns.

    The answer here is byte-for-byte what an ``MCP_SERVER`` pack produces — FastMCP renders a
    returned mapping as one text block of ``json.dumps(payload, indent=2)``, which the
    wrapper hands over as a ``str`` — so the case is the wire shape rather than a mapping the
    runner never sees. Read through ``build_grade_reasons`` as well as off the record,
    because the sentence is what a reader of ``grade.reasons`` is told.
    """
    reported = await _replay_as_the_runner_does(_DECLINED_PAYLOAD, build_tool)

    assert reported.golden_replay.failures == (
        FailedGoldenAction(
            index=0,
            name="confirm_payment",
            kind=GoldenActionFailure.REPORTED,
            error="Order 'O-999' not found",
        ),
    )
    reasons = _reasons_for(reported)
    assert "GOLDEN REPLAY ERRORS:" in reasons, reasons
    assert "1 of 1" in reasons, reasons
    assert "[0] confirm_payment reported Order 'O-999' not found" in reasons, reasons


@pytest.mark.parametrize(
    ("build_tool", "answer", "expected"),
    [
        pytest.param(
            _flagging_tool,
            _FLAGGED_PROSE,
            FailedGoldenAction(
                index=0,
                name="confirm_payment",
                kind=GoldenActionFailure.RAISED,
                error=_FLAGGED_PROSE,
            ),
            id="flagged-prose",
        ),
        pytest.param(
            _flagging_tool,
            _DECLINED_PAYLOAD,
            FailedGoldenAction(
                index=0,
                name="confirm_payment",
                kind=GoldenActionFailure.RAISED,
                error=_DECLINED_PAYLOAD,
            ),
            id="flagged-payload",
        ),
        pytest.param(
            _unflagged_tool,
            _DECLINED_PAYLOAD,
            FailedGoldenAction(
                index=0,
                name="confirm_payment",
                kind=GoldenActionFailure.REPORTED,
                error="Order 'O-999' not found",
            ),
            id="unflagged-payload",
        ),
    ],
)
async def test_the_runner_records_one_failure_under_the_verb_of_whoever_declared_it(
    build_tool: Callable[[str], Any], answer: str, expected: FailedGoldenAction
) -> None:
    """A substrate's own failure flag is the raised kind, and it is read before the payload.

    The flagged rows are the defect this closes: a golden action whose *call* an
    ``MCP_SERVER`` pack failed left the record empty, so the trial was hashed against a world
    that action is missing from with nothing said about it. Raised, because the flag is the
    substrate's and not the tool's — its body never decided anything, exactly as when an
    exception reaches the loop — and the message stays the substrate's own words, the
    ``Error executing tool …`` prefix and the newlines included, so an author reads the layer
    that failed rather than a tidied summary. Core quotes ``TypeError: …`` for the same
    authored defect: the kind, the verb and the sentence's shape are shared, the message is
    whichever layer failed.

    The last two rows carry the same answer and differ only in the flag, which is what pins
    the reading order: a flagged call whose text the payload predicate would *also* read as a
    failure records once, under the flag, and an unflagged one still records what its tool
    reported.
    """
    result = await _replay_as_the_runner_does(answer, build_tool)

    assert result.golden_replay.failures == (expected,)
    reasons = _reasons_for(result)
    assert "GOLDEN REPLAY ERRORS:" in reasons, reasons
    assert "1 of 1" in reasons, reasons
    assert f"[0] confirm_payment {expected.kind.value} {expected.error}" in reasons, reasons


@pytest.mark.parametrize(
    "build_tool",
    [pytest.param(_AnsweringTool, id="execute"), pytest.param(_unflagged_tool, id="execute-call")],
)
async def test_the_runner_records_nothing_for_a_tool_that_answered_a_success_payload(
    build_tool: Callable[[str], Any],
) -> None:
    """The control: the cases above are not a loop that records every action it replays.

    Every payload the runner reads is a ``str``, success and failure alike, so without this
    a predicate applied to the wrong half of that population would pass the cases above and
    annotate every hash-graded trial in the tree. Through both shapes the loop builds an
    outcome from, so a loop reading the failure off the shape rather than off the outcome —
    every ``execute_call`` answer a declared failure — fails it too.
    """
    succeeded = await _replay_as_the_runner_does(
        '{\n  "id": "O-001",\n  "status": "paid"\n}', build_tool
    )

    assert succeeded.golden_replay == GoldenReplayRecord(authored=1)
    assert _reasons_for(succeeded) == "State: hash match"


@pytest.mark.parametrize(
    ("build_tool", "unreadable"),
    [
        pytest.param(_ShapelessTool, "_ShapelessTool", id="no-shape"),
        pytest.param(_AsyncCallTool, "coroutine", id="unawaited-coroutine"),
        pytest.param(_answering_mapping_callable, "dict", id="mapping-answer"),
        pytest.param(_OutcomelessTool, "str", id="execute-call-answering-text"),
    ],
)
async def test_the_runner_records_a_golden_action_whose_answer_it_cannot_read(
    build_tool: Callable[[str], Any], unreadable: str
) -> None:
    """An answer no effect can be read out of is the raised kind, named by the shape at fault.

    The alternative is the record reading as a whole golden path while one of its actions
    took no effect anybody can show — the trial hashed against a world missing that action
    with nothing said about it, which is the defect this whole module is about. So the loop
    refuses the answer instead, and the refusal is what the author is handed: the type of the
    registered object that answered through no shape, or the type of the answer itself where
    the replay reads a ``str`` or a ``ToolCallOutcome``.

    Every row records rather than escaping, which is the second half of the lock: the refusal
    has to happen where the loop's own ``try`` still covers it, so a golden action nobody can
    read annotates one grade instead of ending the whole hash grading.

    The mapping row is also the no-coercion lock. Its answer is a declared failure — the
    payload the pack's declining call really produces, in the mapping shape core is handed —
    so ``str(answer)`` would render it as a Python repr no JSON decodes, ``declared_failure``
    would read no failure in it, and the action a pack declared failed would be recorded as a
    success. Refusing is the only reading that keeps it visible.
    """
    result = await _replay_as_the_runner_does(_DECLINED_PAYLOAD, build_tool)

    assert len(result.golden_replay.failures) == 1, result.golden_replay
    (failure,) = result.golden_replay.failures
    assert failure.index == 0
    assert failure.name == "confirm_payment"
    assert failure.kind == GoldenActionFailure.RAISED
    assert f"'{unreadable}'" in failure.error, failure.error

    reasons = _reasons_for(result)
    assert "GOLDEN REPLAY ERRORS:" in reasons, reasons
    assert "[0] confirm_payment raised TypeError:" in reasons, reasons


def test_hash_unscorable_is_true_when_golden_replay_recorded_a_failure() -> None:
    """A replay that recorded any failure leaves the hash verdict unscorable.

    The world the hash reads is the partial state one broken golden action left behind, so
    a hash score composed against it grades the trial against a state no author asked for.
    :attr:`HashGradingResult.hash_unscorable` is what the runner call site reads before
    writing :attr:`hash_score` into the runner components — ``True`` here keeps the
    ``-1.0`` not-evaluated sentinel and the fold's declared-but-unscored refusal fires
    downstream.
    """
    unscorable = HashGradingResult(
        hash_match=False,
        basis=HashComparisonBasis.GOLDEN_REPLAY,
        golden_replay=GoldenReplayRecord(
            authored=1,
            failures=(
                FailedGoldenAction(
                    index=0,
                    name="confirm_payment",
                    kind=GoldenActionFailure.RAISED,
                    error="the tool raised",
                ),
            ),
        ),
    )

    assert unscorable.hash_unscorable is True


def test_hash_unscorable_is_false_when_golden_replay_ran_whole() -> None:
    """The control: a replay that recorded no failures leaves the hash verdict scorable.

    Without this the runner call site would skip the ``hash_score`` write on every
    hash-graded trial, and every hash pack would land ungradeable — a rule that fires on
    both cells cannot discriminate between them.
    """
    scorable = HashGradingResult(
        hash_match=True,
        basis=HashComparisonBasis.GOLDEN_REPLAY,
        golden_replay=GoldenReplayRecord(authored=2),
    )

    assert scorable.hash_unscorable is False
    assert scorable.hash_score == 1.0
