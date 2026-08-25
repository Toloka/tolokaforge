"""Reserved-name contract for :meth:`CheckRunner.load_checks_module`.

A pack author who writes ``@check\\ndef __executor__(...)`` would produce a
wire ``custom_checks`` list where two entries share ``check_name`` — the
author's check *and* the sentinel emitted by
``composite._executor_error_to_wire``
on a top-level executor failure. The loader refuses the ambiguity at load
time so the sentinel remains unambiguous end-to-end.

The reserved set lives in
:data:`tolokaforge.core.grading.check_runner._RESERVED_CHECK_NAMES`, with
the sentinel value in :data:`_CHECK_EXECUTOR_ERROR_NAME` (the single
source of truth also referenced from
:mod:`tolokaforge.core.grading.composite`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.grading.check_runner import (
    _CHECK_EXECUTOR_ERROR_NAME,
    CheckRunner,
)

pytestmark = pytest.mark.unit


_RESERVED_ONLY_PACK = """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, init, check
)

@init(interface_version="1.0")
def setup(ctx: CheckContext):
    pass

@check
def __executor__():
    return CheckPassed("nope")
"""


_NORMAL_PLUS_RESERVED_PACK = """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, init, check
)

@init(interface_version="1.0")
def setup(ctx: CheckContext):
    pass

@check
def ordinary_check():
    return CheckPassed("ok")

@check
def __executor__():
    return CheckPassed("nope")
"""


def _write_pack(root: Path, body: str) -> Path:
    checks = root / "checks.py"
    checks.write_text(body)
    return checks


class TestReservedNameCollision:
    def test_reserved_only_check_rejected_with_named_message(self, tmp_path: Path) -> None:
        checks_file = _write_pack(tmp_path, _RESERVED_ONLY_PACK)
        runner = CheckRunner()

        with pytest.raises(ValueError) as excinfo:
            runner.load_checks_module(
                checks_file,
                tmp_path,
                relative_imports=[],
                expected_version="1.0",
            )

        message = str(excinfo.value)
        assert _CHECK_EXECUTOR_ERROR_NAME in message
        assert "reserved" in message.lower()

    def test_ordinary_check_next_to_reserved_still_raises_on_reserved(self, tmp_path: Path) -> None:
        # The loader must report the reserved-name collision regardless of
        # decoration order: an unrelated ``@check`` next to the reserved one
        # neither hides nor swallows the failure.
        checks_file = _write_pack(tmp_path, _NORMAL_PLUS_RESERVED_PACK)
        runner = CheckRunner()

        with pytest.raises(ValueError) as excinfo:
            runner.load_checks_module(
                checks_file,
                tmp_path,
                relative_imports=[],
                expected_version="1.0",
            )

        message = str(excinfo.value)
        assert _CHECK_EXECUTOR_ERROR_NAME in message
        assert "ordinary_check" not in message
