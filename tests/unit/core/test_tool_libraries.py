"""Shared tool libraries reach the trial through ``tool_artifacts`` (ADR-0056).

A task pins ``tool_libraries: [{name, version, apps}]``; the engine resolves each
pin among the entry points of ``tolokaforge.tool_libraries``, refuses a missing
library or another version, and :meth:`BaseAdapter.describe_task` merges the
library's bundle into the description whatever the adapter built. The metadata
layer is replaced by an injected entry point, so the real resolver, merge,
:class:`NativeAdapter`, an adapter of another shape and ``tolokaforge validate``
run against a fake library without any installed plug-in.
"""

from __future__ import annotations

import base64
import importlib.metadata
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import plugin_registry
from tolokaforge.core.models import GradingConfig, TaskConfig, ToolLibraryPin
from tolokaforge.core.plugin_registry import TOOL_LIBRARIES_GROUP, available_tool_libraries
from tolokaforge.core.tool_libraries import (
    InMemoryToolLibrary,
    ToolLibrary,
    ToolLibraryError,
    merge_tool_libraries,
    resolve_tool_libraries,
)
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner.models import AdapterType, TaskDescription

pytestmark = pytest.mark.unit

_SHARED = {
    "acme_tools/__init__.py": b"VERSION = '1.2.0'\n",
    "acme_tools/core.py": b"def answer():\n    return 42\n",
}
_APPS = {
    "billing": {"acme_tools/apps/billing.py": b"NAME = 'billing'\n"},
    "calendar": {"acme_tools/apps/calendar.py": b"NAME = 'calendar'\n"},
}


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self.dist = _FakeDist(f"{name}-dist")
        self._target = target

    def load(self) -> object:
        return self._target


InstallLibraries = Callable[..., None]


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallLibraries]:
    """Register ``(name, target)`` pairs as the only ``tolokaforge.tool_libraries`` entries.

    Every other group still reads the real metadata, so an adapter or the CLI
    resolving its own seams is unaffected.
    """
    real_entry_points = importlib.metadata.entry_points

    def _install(*registrations: tuple[str, object]) -> None:
        fakes = [_FakeEntryPoint(name, target) for name, target in registrations]

        def entry_points(**kwargs: Any) -> Any:
            if kwargs.get("group") == TOOL_LIBRARIES_GROUP:
                return fakes
            return real_entry_points(**kwargs)

        monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
        plugin_registry._clear_discovery_cache()

    yield _install
    plugin_registry._clear_discovery_cache()


@pytest.fixture
def acme(install: InstallLibraries) -> InMemoryToolLibrary:
    """``acme_tools`` 1.2.0, the one installed library."""
    library = InMemoryToolLibrary("acme_tools", "1.2.0", shared=_SHARED, apps=_APPS)
    install(("acme_tools", library))
    return library


def _description(**artifacts: bytes) -> TaskDescription:
    return TaskDescription(
        task_id="t",
        name="t",
        category="c",
        description="d",
        adapter_type=AdapterType.NATIVE,
        system_prompt="s",
        tool_artifacts={
            path: base64.b64encode(content).decode("ascii") for path, content in artifacts.items()
        },
    )


def _decoded(description: TaskDescription) -> dict[str, bytes]:
    return {path: base64.b64decode(b64) for path, b64 in description.tool_artifacts.items()}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_pin_resolves_to_the_installed_library(acme: InMemoryToolLibrary) -> None:
    (resolved,) = resolve_tool_libraries([ToolLibraryPin(name="acme_tools", version="1.2.0")])

    assert resolved.library is acme
    assert isinstance(acme, ToolLibrary)
    assert available_tool_libraries() == ["acme_tools"]


def test_another_installed_version_is_refused_naming_both(acme: InMemoryToolLibrary) -> None:
    with pytest.raises(ToolLibraryError, match="pinned at version 1.3.0 but version 1.2.0"):
        resolve_tool_libraries([ToolLibraryPin(name="acme_tools", version="1.3.0")])


def test_a_library_that_is_not_installed_is_refused_naming_what_is(
    acme: InMemoryToolLibrary,
) -> None:
    with pytest.raises(ToolLibraryError, match=r"'zeta_tools' is pinned .*installed: acme_tools"):
        resolve_tool_libraries([ToolLibraryPin(name="zeta_tools", version="1.0")])


def test_with_nothing_installed_the_refusal_says_so(install: InstallLibraries) -> None:
    install()

    with pytest.raises(ToolLibraryError, match=r"installed: none"):
        resolve_tool_libraries([ToolLibraryPin(name="acme_tools", version="1.2.0")])


def test_the_same_library_pinned_twice_is_refused(acme: InMemoryToolLibrary) -> None:
    pin = ToolLibraryPin(name="acme_tools", version="1.2.0")

    with pytest.raises(ToolLibraryError, match="pinned twice"):
        resolve_tool_libraries([pin, pin])


def test_an_entry_point_that_registers_no_library_is_refused(install: InstallLibraries) -> None:
    install(("acme_tools", object()))

    with pytest.raises(ToolLibraryError, match="lacks name, version or bundle"):
        resolve_tool_libraries([ToolLibraryPin(name="acme_tools", version="1.2.0")])


def test_a_library_registered_under_another_name_is_refused(install: InstallLibraries) -> None:
    install(("acme_tools", InMemoryToolLibrary("other_tools", "1.2.0", shared=_SHARED)))

    with pytest.raises(ToolLibraryError, match="registers a tool library named 'other_tools'"):
        resolve_tool_libraries([ToolLibraryPin(name="acme_tools", version="1.2.0")])


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_the_selected_applications_are_merged_with_provenance(acme: InMemoryToolLibrary) -> None:
    pins = [ToolLibraryPin(name="acme_tools", version="1.2.0", apps=["billing"])]

    merged = merge_tool_libraries(_description(**{"mcp_server.py": b"# server\n"}), pins)

    assert _decoded(merged) == {
        "mcp_server.py": b"# server\n",
        **_SHARED,
        **_APPS["billing"],
    }
    assert merged.metadata["tool_libraries"] == [
        {"name": "acme_tools", "version": "1.2.0", "apps": ["billing"], "files": 3}
    ]
    assert acme.calls == [("billing",)]


def test_a_pin_without_apps_bundles_every_application(acme: InMemoryToolLibrary) -> None:
    merged = merge_tool_libraries(
        _description(), [ToolLibraryPin(name="acme_tools", version="1.2.0")]
    )

    assert set(_decoded(merged)) == {*_SHARED, *_APPS["billing"], *_APPS["calendar"]}
    assert merged.metadata["tool_libraries"][0]["apps"] is None
    assert acme.calls == [None]


def test_no_pins_leave_the_description_untouched(acme: InMemoryToolLibrary) -> None:
    description = _description(**{"mcp_server.py": b"# server\n"})

    assert merge_tool_libraries(description, []) is description
    assert "tool_libraries" not in description.metadata


def test_a_path_the_adapter_already_bundles_is_refused(acme: InMemoryToolLibrary) -> None:
    stale_copy = _description(**{"acme_tools/core.py": b"# vendored\n"})

    with pytest.raises(ToolLibraryError, match=r"1 path\(s\) .*acme_tools/core.py.*own copy"):
        merge_tool_libraries(stale_copy, [ToolLibraryPin(name="acme_tools", version="1.2.0")])


def test_two_libraries_bundling_one_path_are_refused(install: InstallLibraries) -> None:
    install(
        ("acme_tools", InMemoryToolLibrary("acme_tools", "1.2.0", shared=_SHARED)),
        ("acme_extra", InMemoryToolLibrary("acme_extra", "0.1.0", shared=_SHARED)),
    )
    pins = [
        ToolLibraryPin(name="acme_tools", version="1.2.0"),
        ToolLibraryPin(name="acme_extra", version="0.1.0"),
    ]

    with pytest.raises(ToolLibraryError, match="'acme_extra' bundles 2 path"):
        merge_tool_libraries(_description(), pins)


def test_an_application_the_library_does_not_ship_is_refused(acme: InMemoryToolLibrary) -> None:
    with pytest.raises(ToolLibraryError, match=r"ships no application \['payroll'\]"):
        merge_tool_libraries(
            _description(),
            [ToolLibraryPin(name="acme_tools", version="1.2.0", apps=["payroll"])],
        )


def test_an_empty_bundle_is_refused(install: InstallLibraries) -> None:
    install(("acme_tools", InMemoryToolLibrary("acme_tools", "1.2.0")))

    with pytest.raises(ToolLibraryError, match="empty bundle for every application"):
        merge_tool_libraries(_description(), [ToolLibraryPin(name="acme_tools", version="1.2.0")])


@pytest.mark.parametrize("path", ["/etc/acme.py", "../acme.py", "acme/../../x.py", "acme\\x.py"])
def test_a_bundle_path_outside_the_artefact_root_is_refused(
    install: InstallLibraries, path: str
) -> None:
    install(("acme_tools", InMemoryToolLibrary("acme_tools", "1.2.0", shared={path: b"x"})))

    with pytest.raises(ToolLibraryError, match="not a relative POSIX path"):
        merge_tool_libraries(_description(), [ToolLibraryPin(name="acme_tools", version="1.2.0")])


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_the_pin_refuses_unknown_keys_and_an_empty_selection() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ToolLibraryPin.model_validate({"name": "acme_tools", "version": "1", "tag": "x"})
    with pytest.raises(ValidationError, match="at least 1 item"):
        ToolLibraryPin(name="acme_tools", version="1", apps=[])


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_a_shared_domain_config_pins_for_every_case(tmp_path: Path) -> None:
    domain = tmp_path / "acme_domain"
    _write_yaml(
        domain / "_shared" / "domain.yaml",
        {"tool_libraries": [{"name": "acme_tools", "version": "1.2.0", "apps": ["billing"]}]},
    )
    _write_yaml(
        domain / "testcases" / "case_a" / "task.yaml",
        {"task_id": "case_a", "description": "d", "domain": "../../_shared/domain.yaml"},
    )

    task, _ = load_task_yaml(domain / "testcases" / "case_a" / "task.yaml")

    assert task.tool_libraries == [
        ToolLibraryPin(name="acme_tools", version="1.2.0", apps=["billing"])
    ]


def test_project_task_defaults_pin_beneath_the_task(tmp_path: Path) -> None:
    task_file = tmp_path / "case_a" / "task.yaml"
    _write_yaml(task_file, {"task_id": "case_a", "description": "d"})
    defaults = {"tool_libraries": [{"name": "acme_tools", "version": "1.2.0"}]}

    task, _ = load_task_yaml(task_file, project_task_defaults=defaults)

    assert task.tool_libraries == [ToolLibraryPin(name="acme_tools", version="1.2.0")]


# ---------------------------------------------------------------------------
# describe_task — every adapter
# ---------------------------------------------------------------------------


def _native_pack(tmp_path: Path, pins: list[dict[str, Any]]) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "acme_pack"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text('{"items": []}')
    (task_dir / "mcp_server.py").write_text("# stub server\n")
    (task_dir / "grading.yaml").write_text("{}\n")
    (task_dir / "fixtures" / "tools.json").write_text(
        json.dumps([{"name": "ping", "description": "p", "parameters": {"type": "object"}}])
    )
    _write_yaml(
        task_dir / "task.yaml",
        {
            "task_id": "acme_pack",
            "name": "acme_pack",
            "category": "tool_use",
            "description": "a pack on a shared library",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"mcp_server": "mcp_server.py", "enabled": ["ping"]}},
            "tool_libraries": pins,
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "system_prompt": "system_prompt.md",
            "grading": "grading.yaml",
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


def test_describe_task_merges_the_pin_into_a_native_pack(
    tmp_path: Path, acme: InMemoryToolLibrary
) -> None:
    adapter = _native_pack(
        tmp_path, [{"name": "acme_tools", "version": "1.2.0", "apps": ["calendar"]}]
    )

    adapters_part = adapter.to_task_description("acme_pack")
    described = adapter.describe_task("acme_pack")

    assert not any(path.startswith("acme_tools/") for path in adapters_part.tool_artifacts)
    assert set(described.tool_artifacts) == {
        *adapters_part.tool_artifacts,
        *_SHARED,
        *_APPS["calendar"],
    }
    assert described.metadata["mcp_server_ref"] == "mcp_server.py"
    assert described.metadata["tool_libraries"] == [
        {"name": "acme_tools", "version": "1.2.0", "apps": ["calendar"], "files": 3}
    ]


def test_describe_task_refuses_a_native_pack_pinned_at_another_version(
    tmp_path: Path, acme: InMemoryToolLibrary
) -> None:
    adapter = _native_pack(tmp_path, [{"name": "acme_tools", "version": "9.9.9"}])

    with pytest.raises(ToolLibraryError, match="pinned at version 9.9.9 but version 1.2.0"):
        adapter.describe_task("acme_pack")


def test_describe_task_refuses_a_native_pack_that_still_carries_the_library(
    tmp_path: Path, acme: InMemoryToolLibrary
) -> None:
    adapter = _native_pack(tmp_path, [{"name": "acme_tools", "version": "1.2.0"}])
    vendored = tmp_path / "tasks" / "acme_pack" / "acme_tools"
    vendored.mkdir()
    (vendored / "__init__.py").write_text("VERSION = 'vendored'\n")

    with pytest.raises(ToolLibraryError, match="acme_tools/__init__.py"):
        adapter.describe_task("acme_pack")


class _AnAdapterOfAnotherShape(BaseAdapter):
    """An adapter that bundles its own host package, as a non-native adapter does.

    Only the two methods :meth:`BaseAdapter.describe_task` reads are implemented;
    every other abstract method raises so a stray read surfaces.
    """

    def __init__(self, pins: list[ToolLibraryPin]) -> None:
        super().__init__({})
        self._pins = pins

    def get_task(self, task_id: str) -> TaskConfig:
        return TaskConfig(task_id=task_id, description="d", tool_libraries=self._pins)

    def to_task_description(self, task_id: str) -> TaskDescription:
        description = _description(**{"host_pkg/__init__.py": b"", "host_pkg/case.py": b"#\n"})
        return description.model_copy(update={"adapter_type": AdapterType.TAU})

    def get_task_ids(self) -> list[str]:
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:
        raise NotImplementedError

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> GradingConfig:
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:
        raise NotImplementedError

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        raise NotImplementedError


def test_describe_task_merges_the_pin_for_an_adapter_of_another_shape(
    acme: InMemoryToolLibrary,
) -> None:
    adapter = _AnAdapterOfAnotherShape(
        [ToolLibraryPin(name="acme_tools", version="1.2.0", apps=["billing"])]
    )

    described = adapter.describe_task("t")

    assert set(described.tool_artifacts) == {
        "host_pkg/__init__.py",
        "host_pkg/case.py",
        *_SHARED,
        *_APPS["billing"],
    }
    assert described.adapter_type == AdapterType.TAU
    assert described.metadata["tool_libraries"][0]["name"] == "acme_tools"


def test_describe_task_without_pins_is_the_adapters_description(
    acme: InMemoryToolLibrary,
) -> None:
    adapter = _AnAdapterOfAnotherShape([])

    assert adapter.describe_task("t") == adapter.to_task_description("t")


# ---------------------------------------------------------------------------
# tolokaforge validate
# ---------------------------------------------------------------------------


def _validate(task_file: Path) -> Any:
    return CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])


def test_validate_reports_a_pin_it_cannot_resolve(
    tmp_path: Path, acme: InMemoryToolLibrary
) -> None:
    _native_pack(tmp_path, [{"name": "acme_tools", "version": "2.0.0"}])

    result = _validate(tmp_path / "tasks" / "acme_pack" / "task.yaml")

    assert result.exit_code == 1, result.stderr
    assert "pinned at version 2.0.0 but version 1.2.0" in result.stderr
    assert "0 valid, 1 invalid" in result.stderr


def test_validate_passes_a_pin_the_engine_can_honour(
    tmp_path: Path, acme: InMemoryToolLibrary
) -> None:
    _native_pack(tmp_path, [{"name": "acme_tools", "version": "1.2.0"}])

    result = _validate(tmp_path / "tasks" / "acme_pack" / "task.yaml")

    assert result.exit_code == 0, result.stderr
    assert "1 valid, 0 invalid" in result.stderr
