"""The engine's side of the trial-observer seam (ADR-0047, packaging amendment): the run identity,
the discovery of the installed plugins and their composition. The receiver-specific behaviour is
the plugin's business (``tolokaforge_langfuse/tests``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core import plugin_registry
from tolokaforge.core.models import ObservabilityConfig, TracingConfig
from tolokaforge.observability.factory import (
    PLUGIN_API_VERSION,
    RUN_IDENTITY_FILE,
    TRIAL_OBSERVERS_GROUP,
    RunIdentity,
    TracingConfigError,
    build_trial_observer,
    installed_plugins,
)
from tolokaforge.observability.observer import (
    CompositeTrialObserver,
    ExportReceipt,
    NullTrialObserver,
)

pytestmark = pytest.mark.unit


class _Observer(NullTrialObserver):
    def __init__(self, name: str) -> None:
        self.name = name

    def run_finished(self) -> ExportReceipt:
        return ExportReceipt(exporter=self.name, spans_exported=1)


class _EntryPoint:
    """What ``importlib.metadata`` hands the factory: a name, a target and ``load()``."""

    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self.target = target
        self.value = f"fake_{name}:build"

    def load(self) -> Any:
        if isinstance(self.target, Exception):
            raise self.target
        return self.target


@pytest.fixture
def plugins(monkeypatch) -> dict[str, _EntryPoint]:
    registry: dict[str, _EntryPoint] = {}

    def discover(group: str):
        assert group == TRIAL_OBSERVERS_GROUP
        return registry

    monkeypatch.setattr(plugin_registry, "discover_entry_points", discover)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_ID", raising=False)
    monkeypatch.delenv("TOLOKAFORGE_TRACING_RUN_TAG", raising=False)
    return registry


def _recording(answer: Any, calls: list[dict[str, Any]]):
    def build(tracing, identity, *, engine_run_id, output_dir):
        calls.append(
            {
                "tracing": tracing,
                "identity": identity,
                "engine_run_id": engine_run_id,
                "output_dir": output_dir,
            }
        )
        return answer

    return build


class TestWithoutAnObserver:
    def test_exporter_none_gives_the_null_observer_and_the_engine_run_id(
        self, plugins, tmp_path: Path
    ) -> None:
        observer, identity = build_trial_observer(
            ObservabilityConfig(), engine_run_id="run-1", output_dir=tmp_path
        )
        assert isinstance(observer, NullTrialObserver)
        assert identity == RunIdentity(run_id="run-1", run_tag="v1")
        assert not (tmp_path / RUN_IDENTITY_FILE).exists()

    def test_no_observability_block_at_all(self, plugins) -> None:
        observer, identity = build_trial_observer(None, engine_run_id="run-1")
        assert isinstance(observer, NullTrialObserver) and identity.run_id == "run-1"

    def test_every_plugin_is_asked_even_when_the_config_is_off(self, plugins) -> None:
        # a plugin may switch itself on from the environment (the Langfuse switch), so the
        # factory asks; a None answer with exporter none is a run without tracing
        calls: list[dict[str, Any]] = []
        plugins["a"] = _EntryPoint("a", _recording(None, calls))
        observer, identity = build_trial_observer(None, engine_run_id="run-1")
        assert isinstance(observer, NullTrialObserver)
        assert calls == [
            {"tracing": None, "identity": identity, "engine_run_id": "run-1", "output_dir": None}
        ]

    def test_otlp_without_any_plugin_is_a_run_start_error(self, plugins) -> None:
        config = ObservabilityConfig(
            tracing=TracingConfig(exporter="otlp", endpoint="http://127.0.0.1:9/v1/traces")
        )
        with pytest.raises(
            TracingConfigError, match="no trial-observer plugin.*tolokaforge-langfuse"
        ):
            build_trial_observer(config, engine_run_id="run-1")

    def test_otlp_that_no_plugin_takes_is_the_same_error(self, plugins) -> None:
        plugins["a"] = _EntryPoint("a", _recording(None, []))
        config = ObservabilityConfig(
            tracing=TracingConfig(exporter="otlp", endpoint="http://127.0.0.1:9/v1/traces")
        )
        with pytest.raises(TracingConfigError, match="installed under.*: a"):
            build_trial_observer(config, engine_run_id="run-1")


class TestWithPlugins:
    def test_one_plugin_is_the_observer_and_the_identity_is_written(
        self, plugins, tmp_path: Path
    ) -> None:
        calls: list[dict[str, Any]] = []
        plugins["a"] = _EntryPoint("a", _recording(_Observer("a"), calls))
        tracing = TracingConfig(
            exporter="otlp", endpoint="http://127.0.0.1:9/v1/traces", run_id="acme/1", run_tag="v2"
        )
        observer, identity = build_trial_observer(
            ObservabilityConfig(tracing=tracing), engine_run_id="engine-run", output_dir=tmp_path
        )
        assert isinstance(observer, _Observer) and observer.name == "a"
        assert identity == RunIdentity(run_id="acme/1", run_tag="v2")
        assert calls[0]["tracing"] is tracing and calls[0]["output_dir"] == tmp_path
        sidecar = json.loads((tmp_path / RUN_IDENTITY_FILE).read_text())
        assert (sidecar["run_id"], sidecar["run_tag"], sidecar["written_by"]) == (
            "acme/1",
            "v2",
            "tolokaforge",
        )
        assert sidecar["engine_version"]

    def test_the_launcher_identity_beats_the_engine_run_id_and_the_config_beats_both(
        self, plugins, monkeypatch
    ) -> None:
        plugins["a"] = _EntryPoint("a", _recording(_Observer("a"), []))
        monkeypatch.setenv("TOLOKAFORGE_TRACING_RUN_ID", "launcher/7")
        monkeypatch.setenv("TOLOKAFORGE_TRACING_RUN_TAG", "v3")
        _, identity = build_trial_observer(None, engine_run_id="engine-run")
        assert identity == RunIdentity(run_id="launcher/7", run_tag="v3")
        tracing = TracingConfig(exporter="otlp", endpoint="http://x/v1/traces", run_id="cfg/1")
        _, identity = build_trial_observer(
            ObservabilityConfig(tracing=tracing), engine_run_id="engine-run"
        )
        assert identity == RunIdentity(run_id="cfg/1", run_tag="v3")

    def test_two_plugins_compose_in_name_order(self, plugins) -> None:
        plugins["b"] = _EntryPoint("b", _recording(_Observer("b"), []))
        plugins["a"] = _EntryPoint("a", _recording(_Observer("a"), []))
        observer, _ = build_trial_observer(None, engine_run_id="run-1")
        assert isinstance(observer, CompositeTrialObserver)
        assert [o.name for o in observer.observers] == ["a", "b"]
        receipt = observer.run_finished()
        assert (receipt.exporter, receipt.spans_exported) == ("a, b", 2)

    def test_a_plugin_that_cannot_load_is_a_configuration_error(self, plugins) -> None:
        plugins["broken"] = _EntryPoint("broken", ImportError("No module named 'opentelemetry'"))
        with pytest.raises(TracingConfigError, match="'broken'.*fake_broken:build.*opentelemetry"):
            build_trial_observer(None, engine_run_id="run-1")

    def test_a_plugins_configuration_error_stops_the_run_start(self, plugins) -> None:
        def refuse(tracing, identity, *, engine_run_id, output_dir):
            raise TracingConfigError("expect_project mismatch")

        plugins["a"] = _EntryPoint("a", refuse)
        with pytest.raises(TracingConfigError, match="expect_project mismatch"):
            build_trial_observer(None, engine_run_id="run-1")

    def test_installed_plugins_are_listed_by_name(self, plugins) -> None:
        plugins["z"] = _EntryPoint("z", None)
        plugins["a"] = _EntryPoint("a", None)
        assert installed_plugins() == ["a", "z"]


class TestTheContract:
    def test_the_plugin_api_version_is_the_documented_one(self) -> None:
        assert PLUGIN_API_VERSION == 1

    def test_the_langfuse_plugin_is_installed_in_this_workspace(self) -> None:
        # the workspace member registers itself; the engine finds it without importing it
        assert "langfuse" in installed_plugins()

    def test_run_id_components_are_validated(self, plugins) -> None:
        with pytest.raises(TracingConfigError):
            build_trial_observer(None, engine_run_id="bad|id")
