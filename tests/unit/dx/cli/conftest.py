"""Shared fixtures for the ``tolokaforge grade`` / ``tolokaforge grade-run`` CLI tests.

- :func:`stored_bundle` — builds a v1.0 grade bundle via
  :func:`serialize_grade_bundle`, uploads it through a
  :class:`LocalDiskBundleStore`, and returns ``(uri, store_root)``.
- :func:`register_grader_kind` — factory that installs a test-only kind
  under the ``tolokaforge.grader_kinds`` entry-point group by patching
  :func:`plugin_registry.discover_entry_points`, so a CliRunner invocation
  resolves to the fixture kind without touching pyproject metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.bundle import serialize_grade_bundle
from tolokaforge.core.grading.bundle_store import LocalDiskBundleStore
from tolokaforge.core.models.grade import Grade

_TRIAL_ID = "trial-fixture"


def _write_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.py").write_bytes(b"print('hello')\n")
    (root / "README.md").write_bytes(b"# fixture\n")


@pytest.fixture
def stored_bundle(tmp_path: Path) -> tuple[str, Path]:
    """Serialise a synthetic bundle, put it into a local-disk store, return URI + root."""
    workspace = tmp_path / "workspace"
    _write_workspace(workspace)

    bundle_dir = tmp_path / "bundle"
    serialize_grade_bundle(
        bundle_dir,
        trial_id=_TRIAL_ID,
        initial_state={"tables": {"users": []}},
        final_state={"tables": {"users": [{"id": 1}]}},
        final_state_stable={"tables": {"users": [{"id": 1}]}},
        filesystem_root=workspace,
        checks=None,
        kb=None,
        trajectory={"llm_messages": []},
        grading_config={"combine_method": "weighted", "weights": {"custom": 1.0}},
    )
    store_root = tmp_path / "store"
    store = LocalDiskBundleStore(root_dir=store_root)
    uri = store.put(bundle_dir)
    store.close()
    return uri, store_root


@pytest.fixture
def test_execution_bundle(tmp_path: Path) -> tuple[str, Path]:
    """A bundle whose grading_config declares ``grading_method: test_execution``."""
    workspace = tmp_path / "workspace-te"
    _write_workspace(workspace)

    bundle_dir = tmp_path / "bundle-te"
    serialize_grade_bundle(
        bundle_dir,
        trial_id=_TRIAL_ID,
        initial_state={"tables": {}},
        final_state={"tables": {}},
        final_state_stable={"tables": {}},
        filesystem_root=workspace,
        checks=None,
        kb=None,
        trajectory={"llm_messages": []},
        grading_config={
            "combine_method": "weighted",
            "weights": {},
            "grading_method": "test_execution",
        },
    )
    store_root = tmp_path / "store-te"
    store = LocalDiskBundleStore(root_dir=store_root)
    uri = store.put(bundle_dir)
    store.close()
    return uri, store_root


def _fake_entry_point(name: str, target: object) -> Any:
    class _FakeEP:
        def __init__(self) -> None:
            self.name = name

        def load(self) -> object:
            return target

    return _FakeEP()


@pytest.fixture
def register_grader_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str, type], None]]:
    """Install a fake kind entry-point under ``tolokaforge.grader_kinds``.

    The plugin_registry caches discovery per-group. We patch the discovery
    call to layer the fixture kind on top of the real registry, then clear
    the cache so subsequent lookups see the union. Cache clears on teardown.
    """
    from tolokaforge.core import plugin_registry as pr

    original_discover = pr.discover_entry_points
    injected: dict[str, object] = {}

    def scoped_discover(group: str) -> Mapping[str, Any]:
        base = original_discover(group)
        if group == pr.GRADER_KINDS_GROUP and injected:
            merged = dict(base)
            for name, target in injected.items():
                merged[name] = _fake_entry_point(name, target)
            return merged
        return base

    monkeypatch.setattr(pr, "_discovery_cache", {})
    monkeypatch.setattr(pr, "discover_entry_points", scoped_discover)

    def register(name: str, cls: type) -> None:
        injected[name] = cls
        pr._clear_discovery_cache()

    try:
        yield register
    finally:
        pr._clear_discovery_cache()


class _FixedScoreKind:
    """Kind returning a canned :class:`Grade` regardless of substrate."""

    NAME = "_fixed_score"

    _canned = Grade(binary_pass=True, score=0.42, reasons="fixture verdict")

    def evaluate(self, **_: object) -> Grade:
        return self._canned


class _RefusingTestKind:
    NAME = "_refusing"

    def evaluate(self, **_: object) -> Grade:
        from tolokaforge.core.grading.kinds import GraderKindRefusedError

        raise GraderKindRefusedError("no exec tool")


def canned_grade() -> Grade:
    return _FixedScoreKind._canned
