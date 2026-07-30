"""Registry-race lock for :class:`CheckRunner.load_checks_module`.

The check registry in
:mod:`~tolokaforge.core.grading.checks_interface` is module-global —
``@check`` and ``@init`` fire at import time and have no reference to a
runner instance. The gRPC handler pool serves concurrent trials; without
serialisation, two of them calling ``load_checks_module`` in parallel
would interleave ``reset_registry → exec_module → get_registered_checks``
and each caller would see a merged/partial registry.
:data:`~tolokaforge.core.grading.check_runner._CHECKS_MODULE_LOAD_LOCK`
serialises the sequence per-process so every caller sees only its own
module's checks.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CustomChecksConfig,
    EnvironmentState,
    TaskContext,
    Transcript,
)

pytestmark = pytest.mark.unit


_PACK_TEMPLATE = """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, init, check
)

@init(interface_version="1.0")
def setup(ctx: CheckContext):
    pass

@check
def check_only_in_{name}():
    return CheckPassed("ok")
"""


def _write_pack(root: Path, name: str) -> Path:
    """Write a minimal ``checks.py`` under ``root`` with a uniquely-named check."""
    pack_dir = root / f"pack_{name}"
    pack_dir.mkdir()
    checks = pack_dir / "checks.py"
    checks.write_text(_PACK_TEMPLATE.format(name=name))
    return checks


class TestConcurrentModuleLoad:
    def test_concurrent_runs_do_not_cross_contaminate(self, tmp_path: Path) -> None:
        """Two threads, two distinct packs, N iterations each — each caller
        sees only its own check name in every :class:`CheckResultSet`.
        """
        checks_a = _write_pack(tmp_path, "alpha")
        checks_b = _write_pack(tmp_path, "beta")
        config = CustomChecksConfig(enabled=True, timeout_seconds=5, interface_version="1.0")
        ctx = CheckContext(
            initial_state=EnvironmentState(data={}),
            final_state=EnvironmentState(data={}),
            transcript=Transcript(messages=[]),
            task=TaskContext(task_id="race"),
        )

        def _run(pack: Path, expected_check: str) -> list[str]:
            runner = CheckRunner()
            observed: list[str] = []
            for _ in range(50):
                result = runner.run(pack, pack.parent, ctx, config)
                if result.error is not None:
                    raise AssertionError(f"unexpected executor error: {result.error}")
                names = sorted(r.check_name for r in result.results)
                observed.append(",".join(names))
                if names != [expected_check]:
                    raise AssertionError(
                        f"cross-contamination: expected [{expected_check}] but saw {names}"
                    )
            return observed

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(_run, checks_a, "check_only_in_alpha")
            fut_b = pool.submit(_run, checks_b, "check_only_in_beta")
            observed_a = fut_a.result(timeout=30)
            observed_b = fut_b.result(timeout=30)

        assert all(entry == "check_only_in_alpha" for entry in observed_a)
        assert all(entry == "check_only_in_beta" for entry in observed_b)
