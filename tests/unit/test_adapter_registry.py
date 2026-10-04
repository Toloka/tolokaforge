"""Unit tests for the multi-harness resolver (issue #1750, slice A2).

Covers :class:`CompositeAdapter` and :func:`build_composite_adapter`:

- ``for_entry`` routes to the per-entry adapter,
- the union accessors aggregate across entries (docker-CLI OR, docker-stack
  merge, fingerprints per distinct adapter type, grader-name agreement),
- ``for_entry`` on a plain single adapter returns self,
- the builder's overlap guard fires when one task id spans two entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters import (
    BaseAdapter,
    DockerStackRequirements,
    register_adapter,
)
from tolokaforge.adapters.base import AdapterEnvironment, ComposeImageBuild
from tolokaforge.core.adapter_registry import (
    CompositeAdapter,
    HarnessEntry,
    build_composite_adapter,
)
from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models.run_config import HarnessEntryConfig

pytestmark = pytest.mark.unit


class _FakeAdapter(BaseAdapter):
    """Minimal adapter whose run-level knobs are set per subclass."""

    _task_ids: list[str] = []
    _fingerprint: dict[str, Any] | None = None
    _stack: DockerStackRequirements | None = None

    def get_task_ids(self) -> list[str]:
        return list(self._task_ids)

    def get_task(self, task_id: str) -> Any:
        return {"task_id": task_id, "adapter": type(self).__name__}

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/tmp") / task_id

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        return AdapterEnvironment(data={}, tools=[], wiki="", rules=[])

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    def get_system_prompt(self, task_id: str) -> str:
        return ""

    def get_grading_config(self, task_id: str) -> Any:
        return None

    def reset_environment(self, env: AdapterEnvironment) -> None:
        return None

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        return None

    def to_task_description(self, task_id: str) -> Any:
        return {"task_id": task_id}

    def fingerprint(self) -> dict[str, Any] | None:
        return self._fingerprint

    def docker_stack_requirements(self) -> DockerStackRequirements:
        return self._stack if self._stack is not None else DockerStackRequirements()


class _PlainAdapter(_FakeAdapter):
    _task_ids = ["a1", "a2"]


class _DelegatedAdapter(_FakeAdapter):
    _task_ids = ["d1"]
    _fingerprint = {"kind": "delegated", "rev": "abc"}
    requires_docker_cli_in_runner = True
    supported_execution_modes = frozenset({ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED})

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self._stack = DockerStackRequirements(
            mount_docker_socket=True,
            needs_rag_service=True,
            extra_runner_binds=[(Path("/host/logs"), "/logs")],
        )


def _entry(name: str, adapter: BaseAdapter, adapter_type: str, task_ids: list[str]) -> HarnessEntry:
    return HarnessEntry(
        name=name,
        config=HarnessEntryConfig(name=name, adapter=adapter_type),
        adapter=adapter,
        task_ids=task_ids,
    )


class TestForEntryRouting:
    def test_for_entry_returns_the_entrys_adapter(self) -> None:
        native = _PlainAdapter({})
        delegated = _DelegatedAdapter({})
        composite = CompositeAdapter(
            [
                _entry("native", native, "native", ["a1", "a2"]),
                _entry("deleg", delegated, "fake_delegated", ["d1"]),
            ]
        )
        assert composite.for_entry("native") is native
        assert composite.for_entry("deleg") is delegated

    def test_for_entry_unknown_raises(self) -> None:
        composite = CompositeAdapter([_entry("native", _PlainAdapter({}), "native", ["a1"])])
        with pytest.raises(KeyError, match="No harness entry named"):
            composite.for_entry("missing")

    def test_for_entry_on_plain_adapter_returns_self(self) -> None:
        # The inherited BaseAdapter.for_entry default is a no-op for every
        # single-adapter run; NativeAdapter needs real params to construct, so
        # exercise the default via a plain BaseAdapter subclass.
        plain = _PlainAdapter({})
        assert plain.for_entry("anything") is plain


class TestUnionAccessors:
    def _composite(self) -> CompositeAdapter:
        return CompositeAdapter(
            [
                _entry("native", _PlainAdapter({}), "native", ["a1", "a2"]),
                _entry("deleg", _DelegatedAdapter({}), "fake_delegated", ["d1"]),
            ]
        )

    def test_any_requires_docker_cli_is_an_or(self) -> None:
        assert self._composite().any_requires_docker_cli() is True

    def test_any_requires_docker_cli_false_when_none_need_it(self) -> None:
        composite = CompositeAdapter(
            [
                _entry("p1", _PlainAdapter({}), "native", ["a1"]),
                _entry("p2", _PlainAdapter({}), "native", ["a2"]),
            ]
        )
        assert composite.any_requires_docker_cli() is False

    def test_union_docker_stack_requirements_merges(self) -> None:
        merged = self._composite().union_docker_stack_requirements()
        assert merged.mount_docker_socket is True
        assert merged.needs_rag_service is True
        assert (Path("/host/logs"), "/logs") in merged.extra_runner_binds

    def test_union_docker_stack_requirements_conflict_raises(self) -> None:
        a = _DelegatedAdapter({})
        b = _DelegatedAdapter({})
        b._stack = DockerStackRequirements(extra_runner_binds=[(Path("/other"), "/logs")])
        composite = CompositeAdapter(
            [
                _entry("a", a, "fake_delegated", ["d1"]),
                _entry("b", b, "fake_delegated", ["d2"]),
            ]
        )
        with pytest.raises(ValueError, match="conflict"):
            composite.union_docker_stack_requirements()

    def test_image_build_conflict_raises(self) -> None:
        a = _DelegatedAdapter({})
        a._stack = DockerStackRequirements(
            image_builds=[ComposeImageBuild(Path("/c.yaml"), "svc", "img:1")]
        )
        b = _DelegatedAdapter({})
        b._stack = DockerStackRequirements(
            image_builds=[ComposeImageBuild(Path("/c.yaml"), "svc", "img:2")]
        )
        composite = CompositeAdapter(
            [
                _entry("a", a, "fake_delegated", ["d1"]),
                _entry("b", b, "fake_delegated", ["d2"]),
            ]
        )
        with pytest.raises(ValueError, match="pinned refs"):
            composite.union_docker_stack_requirements()

    def test_fingerprints_by_type(self) -> None:
        fps = self._composite().fingerprints_by_type()
        # native reports nothing (omitted); the delegated type reports a payload.
        assert fps == {"fake_delegated": {"kind": "delegated", "rev": "abc"}}

    def test_agreed_trial_grader_name_agrees(self) -> None:
        # Both fakes inherit BaseAdapter.trial_grader_name = "runner_rpc".
        assert self._composite().agreed_trial_grader_name() == "runner_rpc"

    def test_agreed_trial_grader_name_disagreement_raises(self) -> None:
        class _OtherGrader(_PlainAdapter):
            trial_grader_name = "grader_rpc"

        composite = CompositeAdapter(
            [
                _entry("a", _PlainAdapter({}), "native", ["a1"]),
                _entry("b", _OtherGrader({}), "native", ["a2"]),
            ]
        )
        with pytest.raises(ValueError, match="disagree on their default trial grader"):
            composite.agreed_trial_grader_name()


class TestAmbiguousSurfacesRaise:
    def _composite(self) -> CompositeAdapter:
        return CompositeAdapter([_entry("native", _PlainAdapter({}), "native", ["a1"])])

    def test_bare_trial_grader_name_raises(self) -> None:
        with pytest.raises(RuntimeError, match="ambiguous"):
            _ = self._composite().trial_grader_name

    def test_bare_docker_cli_flag_raises(self) -> None:
        with pytest.raises(RuntimeError, match="ambiguous"):
            _ = self._composite().requires_docker_cli_in_runner

    def test_docker_stack_requirements_method_raises(self) -> None:
        with pytest.raises(RuntimeError, match="ambiguous"):
            self._composite().docker_stack_requirements()

    def test_per_task_calls_raise(self) -> None:
        composite = self._composite()
        with pytest.raises(RuntimeError, match="for_entry"):
            composite.get_task("a1")
        with pytest.raises(RuntimeError, match="for_entry"):
            composite.get_task_ids()


class TestBuilderOverlapGuard:
    def test_builder_routes_and_enumerates(self) -> None:
        register_adapter("fake_plain_a2", _PlainAdapter)
        register_adapter("fake_deleg_a2", _DelegatedAdapter)
        composite = build_composite_adapter(
            [
                HarnessEntryConfig(name="p", adapter="fake_plain_a2"),
                HarnessEntryConfig(name="d", adapter="fake_deleg_a2"),
            ],
            params_for_entry=lambda cfg: {},
        )
        assert composite.entries["p"].task_ids == ["a1", "a2"]
        assert composite.entries["d"].task_ids == ["d1"]
        assert isinstance(composite.for_entry("p"), _PlainAdapter)

    def test_task_id_allow_list_filters(self) -> None:
        register_adapter("fake_plain_a2b", _PlainAdapter)
        composite = build_composite_adapter(
            [HarnessEntryConfig(name="p", adapter="fake_plain_a2b", task_ids=["a2"])],
            params_for_entry=lambda cfg: {},
        )
        assert composite.entries["p"].task_ids == ["a2"]

    def test_overlap_guard_fires(self) -> None:
        register_adapter("fake_plain_ov", _PlainAdapter)
        with pytest.raises(ValueError, match="1768"):
            build_composite_adapter(
                [
                    HarnessEntryConfig(name="p1", adapter="fake_plain_ov"),
                    HarnessEntryConfig(name="p2", adapter="fake_plain_ov"),
                ],
                params_for_entry=lambda cfg: {},
            )
