"""What a golden replay does with an action name that resolves to nothing.

Both substrates, because both replay golden actions and each resolves a name under its
own rule: the core engine against the pack's ``TOOLS`` map exactly, the runner against
the tools it registered for the trial with a single ``…_<name>`` suffix allowed on top.

The core half is driven over the real ``tests/data/tasks/shop_orders_02`` pack — its
``mcp_server.py``, its ``initial_state.json``, and the two-step golden path its
``grading.yaml`` declares. The pack's own ``TOOLS`` map is what decides which names
resolve, so a stand-in map would lock the stand-in's rule rather than the one a replay
actually applies; and the trial states below are produced by driving the pack's tools,
which is how an agent produces them.

The measured defect these cases close: with ``confirm_payment`` misspelled, the replay
skipped it and returned the order still ``pending``, so a trial that placed the order
and never paid — the wrong behaviour — hashed equal to that partial world and scored
``1.0`` "State hash matches", while a correct trial scored ``0.0`` against a diff that
named nothing about the typo. A runner that resolved a name inside its replay loop
rather than ahead of it scores the trial against the same partial world and names the
defect only in a ``GOLDEN REPLAY ERRORS:`` tail on the reasons; the runner cases below
rule that out.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.grading.golden_replay import (
    UnresolvableGoldenAction,
    resolve_golden_action_names,
)
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.runner.models import GoldenAction, TaskDescription
from tolokaforge.runner.service import (
    RunnerServiceImpl,
    TrialContextRuntime,
    _tool_registered_for_trial,
)

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


def _trial_state(pack_tools: dict[str, Any], *calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """The database an agent that made exactly these calls leaves behind."""
    data = json.loads((_TASK_DIR / _INITIAL_STATE).read_text())
    for name, kwargs in calls:
        pack_tools[name].invoke(data=data, **kwargs)
    return data


def _check(
    db_state: dict[str, Any], actions: list[dict[str, Any]]
) -> tuple[float, str, dict[str, Any] | None]:
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
    """The negative control: resolving before executing moves no correct pack's verdict."""
    paid = _trial_state(pack_tools, _PLACE_ORDER, _CONFIRM_PAYMENT)

    score, reason, diff = _check(paid, copy.deepcopy(golden_actions))

    assert (score, diff) == (1.0, None)
    assert reason == "State hash matches"


def test_the_pack_as_authored_still_fails_a_trial_that_never_paid(
    pack_tools, golden_actions
) -> None:
    never_paid = _trial_state(pack_tools, _PLACE_ORDER)

    score, reason, diff = _check(never_paid, copy.deepcopy(golden_actions))

    assert score == 0.0
    assert "State hash mismatch" in reason
    assert diff is not None


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
            context.trial_id, context, [GoldenAction(tool_name="place_ordr")]
        )


async def test_a_resolvable_name_reaches_the_db_client_the_refusal_guards() -> None:
    """The control: the test above passes because resolution precedes the db calls.

    Without this, a hash path that had stopped touching the database at all — or a
    resolution pass that raised on every name — would satisfy the invariant vacuously.
    """
    service, context = _runner_over_a_refusing_db()

    with pytest.raises(AssertionError, match="ran before golden actions resolved"):
        await service._execute_hash_grading(
            context.trial_id, context, [GoldenAction(tool_name="place_order")]
        )
