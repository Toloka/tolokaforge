"""Honesty lock for the runner's advertised adapter surface.

``BUILTIN_ADAPTERS`` is a runner-owned constant so the runner never imports
``tolokaforge.adapters`` to report its capabilities. These tests pin it to the
built-in native adapter and confirm native stays resolvable by the adapters
package, so the runner can never advertise an adapter the host cannot resolve.
"""

from __future__ import annotations

import pytest

from tolokaforge.adapters import available_adapters
from tolokaforge.runner.capabilities import BUILTIN_ADAPTERS
from tolokaforge.runner.models import AdapterType

pytestmark = pytest.mark.canonical


def test_builtin_adapters_is_exactly_native() -> None:
    assert tuple(BUILTIN_ADAPTERS) == (AdapterType.NATIVE.value,)


def test_builtin_adapters_are_resolvable_by_adapters_package() -> None:
    assert set(BUILTIN_ADAPTERS) <= set(available_adapters())
