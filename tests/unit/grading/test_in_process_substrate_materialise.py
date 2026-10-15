"""``InProcessGradingSubstrate`` delegation of the separate-verifier primitives."""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    RunTestSuiteResult,
    SubstrateUnreachableError,
)

pytestmark = pytest.mark.unit


def _substrate(**impls: Any) -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=object(),  # type: ignore[arg-type]
        knowledge_search=None,
        filesystem_root=None,
        **impls,
    )


def test_run_test_suite_threads_service_to_impl() -> None:
    seen: dict[str, Any] = {}

    def impl(**kwargs: Any) -> RunTestSuiteResult:
        seen.update(kwargs)
        return RunTestSuiteResult(
            exit_code=0,
            reward_bytes=b"1\n",
            stdout="",
            tool_absent=False,
            tool_absent_reason="",
            script_exec_error="",
        )

    sub = _substrate(run_test_suite_impl=impl)
    sub.run_test_suite(
        script_path="/tests/test.sh",
        reward_path="/logs/verifier/reward.txt",
        timeout_s=1.0,
        reward_read_timeout_s=1.0,
        service="verifier",
    )
    assert seen["service"] == "verifier"


def test_materialise_path_delegates_to_impl() -> None:
    seen: dict[str, Any] = {}

    def impl(**kwargs: Any) -> None:
        seen.update(kwargs)

    sub = _substrate(materialise_path_impl=impl)
    sub.materialise_path(
        src_service=None,
        dst_service="verifier",
        path="/app/cracker.py",
        exclude=["*.pyc"],
    )
    assert seen == {
        "src_service": None,
        "dst_service": "verifier",
        "path": "/app/cracker.py",
        "exclude": ("*.pyc",),
    }


def test_materialise_path_without_impl_is_unreachable() -> None:
    sub = _substrate()
    with pytest.raises(SubstrateUnreachableError):
        sub.materialise_path(src_service=None, dst_service="verifier", path="/app")
