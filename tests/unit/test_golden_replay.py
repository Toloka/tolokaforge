"""What a golden replay does with an action name that resolves to nothing.

Driven over the real ``tests/data/tasks/shop_orders_02`` pack — its ``mcp_server.py``,
its ``initial_state.json``, and the two-step golden path its ``grading.yaml`` declares.
The pack's own ``TOOLS`` map is what decides which names resolve, so a stand-in map
would lock the stand-in's rule rather than the one a replay actually applies; and the
trial states below are produced by driving the pack's tools, which is how an agent
produces them.

The measured defect these cases close: with ``confirm_payment`` misspelled, the replay
skipped it and returned the order still ``pending``, so a trial that placed the order
and never paid — the wrong behaviour — hashed equal to that partial world and scored
``1.0`` "State hash matches", while a correct trial scored ``0.0`` against a diff that
named nothing about the typo.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.grading.golden_replay import UnresolvableGoldenAction
from tolokaforge.core.grading.state_checks import StateChecker

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
