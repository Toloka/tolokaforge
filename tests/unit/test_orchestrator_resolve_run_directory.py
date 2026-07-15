"""Unit tests locking :func:`tolokaforge.core.orchestrator.resolve_run_directory`.

The helper computes the ``(run_id, output_dir)`` pair a run will use.
Callers resolve the pair before starting the orchestrator so downstream
consumers (banner rendering, external tooling) can address the run
directory by absolute path immediately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.core import orchestrator as orchestrator_module
from tolokaforge.core.orchestrator import resolve_run_directory

pytestmark = pytest.mark.unit


_TS_PATTERN = re.compile(r"^[A-Za-z0-9_]+_\d{8}_\d{6}$")


class TestResolveRunDirectory:
    def test_returns_run_id_and_output_dir(self) -> None:
        run_id, output_dir = resolve_run_directory("results/my_run")
        assert _TS_PATTERN.match(run_id), f"unexpected run_id shape: {run_id!r}"
        assert run_id.startswith("my_run_")
        assert output_dir == Path("results") / run_id

    def test_deterministic_pair_under_frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``datetime.now()`` is frozen the pair is fully determined.

        Locks: ``datetime.now()`` is called exactly once per invocation so
        ``run_id`` and ``output_dir`` agree on the timestamp.
        """

        class _FrozenClock:
            @staticmethod
            def now() -> _FrozenClock:
                return _FrozenClock()

            @staticmethod
            def strftime(fmt: str) -> str:
                assert fmt == "%Y%m%d_%H%M%S"
                return "20260715_120000"

        monkeypatch.setattr(orchestrator_module, "datetime", _FrozenClock)
        run_id, output_dir = resolve_run_directory("results/my_run")
        assert run_id == "my_run_20260715_120000"
        assert output_dir == Path("results/my_run_20260715_120000")

    def test_successive_calls_advance_with_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stamps = iter(["20260715_120000", "20260715_120001"])

        class _AdvancingClock:
            @staticmethod
            def now() -> _AdvancingClock:
                return _AdvancingClock()

            @staticmethod
            def strftime(fmt: str) -> str:
                assert fmt == "%Y%m%d_%H%M%S"
                return next(stamps)

        monkeypatch.setattr(orchestrator_module, "datetime", _AdvancingClock)
        first_id, _ = resolve_run_directory("results/my_run")
        second_id, _ = resolve_run_directory("results/my_run")
        assert first_id != second_id
        assert first_id.endswith("_20260715_120000")
        assert second_id.endswith("_20260715_120001")

    @pytest.mark.parametrize("empty_basename", [".", "/", ""])
    def test_empty_basename_raises_valueerror(self, empty_basename: str) -> None:
        with pytest.raises(ValueError, match="evaluation.output_dir"):
            resolve_run_directory(empty_basename)

    def test_accepts_path_and_string(self) -> None:
        by_string = resolve_run_directory("results/my_run")
        by_path = resolve_run_directory(Path("results/my_run"))
        # Same shape regardless of caller-passed type.
        assert by_string[1].parent == by_path[1].parent

    def test_output_dir_parent_matches_input_parent(self) -> None:
        _, output_dir = resolve_run_directory("/abs/results/experiment")
        assert output_dir.parent == Path("/abs/results")

    def test_output_dir_is_not_resolved(self, tmp_path: Path) -> None:
        """The helper leaves ``output_dir`` unresolved so callers can
        decide when to hit the filesystem (banner resolution vs orchestrator
        ``.mkdir``).
        """

        _, output_dir = resolve_run_directory("results/my_run")
        # A relative input yields a relative output — no resolve() applied.
        assert not output_dir.is_absolute()
