"""The τ³ half-duplex step and environment-error boundaries."""

import pytest

from tolokaforge.core.models import TerminationReason
from tolokaforge.core.simulation_budget import SimulationBudget

pytestmark = pytest.mark.unit


def test_non_environment_turn_stops_on_exact_step() -> None:
    budget = SimulationBudget(max_steps=200, max_errors=10)
    for _ in range(199):
        assert budget.participant(calls_environment=False) is None
    assert budget.participant(calls_environment=False) is TerminationReason.MAX_STEPS
    assert budget.steps == 200


def test_batch_completes_at_boundary_and_errors_win_a_tie() -> None:
    budget = SimulationBudget(max_steps=2, max_errors=10)
    assert budget.participant(calls_environment=True) is None
    assert budget.steps == 1
    assert budget.environment(errors=10) is TerminationReason.TOO_MANY_ERRORS
    assert budget.steps == 2
    assert budget.errors == 10


def test_nine_errors_continue_and_tenth_stops() -> None:
    budget = SimulationBudget(max_steps=200, max_errors=10)
    for _ in range(9):
        budget.participant(calls_environment=True)
        assert budget.environment(errors=1) is None
    budget.participant(calls_environment=True)
    assert budget.environment(errors=1) is TerminationReason.TOO_MANY_ERRORS
    assert budget.steps == 20
