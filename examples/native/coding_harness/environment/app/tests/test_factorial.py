import pytest
from factorial import factorial


def test_factorial_zero() -> None:
    assert factorial(0) == 1


def test_factorial_one() -> None:
    assert factorial(1) == 1


def test_factorial_two() -> None:
    assert factorial(2) == 2


def test_factorial_five() -> None:
    assert factorial(5) == 120


def test_factorial_ten() -> None:
    assert factorial(10) == 3628800


def test_factorial_negative_raises() -> None:
    with pytest.raises(ValueError):
        factorial(-1)
