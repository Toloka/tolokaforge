"""Model-name resolution as configuration and the observer factory (ADR-0046)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.models import ObservabilityConfig, TracingConfig
from tolokaforge.observability.factory import (
    RUN_IDENTITY_FILE,
    RunIdentity,
    TracingConfigError,
    build_trial_observer,
    validate_tag,
)
from tolokaforge.observability.model_names import (
    ModelNameResolverError,
    RawModelNameResolver,
    build_model_name_resolver,
)
from tolokaforge.observability.observer import NullTrialObserver

pytestmark = pytest.mark.unit


class TestRawResolver:
    def test_vendor_model_name_is_kept_and_tagged(self) -> None:
        identity = RawModelNameResolver().resolve("openrouter", "openai/gpt-6-astra")
        assert identity.canonical == "openai/gpt-6-astra"
        assert identity.tags == ("model:openai/gpt-6-astra",)
        assert identity.metadata["model_vendor"] == "openai"
        assert identity.metadata["model_route"] == "openrouter"
        assert identity.metadata["model_family"] == "none"

    def test_bare_name_takes_the_provider_as_vendor(self) -> None:
        assert (
            RawModelNameResolver().resolve("meta", "muse-spark-1.2").canonical
            == "meta/muse-spark-1.2"
        )

    def test_rules_without_the_normalizer_are_refused(self) -> None:
        with pytest.raises(ModelNameResolverError):
            build_model_name_resolver("none", "/tmp/rules.toml")


class TestNormalizerResolver:
    def test_normalizer_identity_and_facets(self, tmp_path: Path) -> None:
        pytest.importorskip("toloka_model_name_normalizer")
        rules = tmp_path / "rules.toml"
        rules.write_text(
            'schema_version = 1\nversion = "test"\n[lookup."tencent/hy3"]\nfamily = "hunyuan"\nwhy = "test"\n'
        )
        resolver = build_model_name_resolver("toloka", str(rules))
        identity = resolver.resolve("openrouter", "tencent/hy3")
        assert identity.canonical == "tencent/hy3"
        assert "model_family:hunyuan" in identity.tags
        assert identity.metadata["model_rules_version"] == "test"
        assert identity.metadata["model_lookup"] is True


class TestTracingConfig:
    def test_defaults_are_off_and_locked(self) -> None:
        tracing = TracingConfig()
        assert (tracing.exporter, tracing.endpoint, tracing.run_tag) == ("none", None, "v1")
        assert tracing.model_name_normalizer == "none" and tracing.model_name_rules is None
        assert tracing.tags == [] and tracing.metadata == {}

    def test_otlp_requires_an_endpoint(self) -> None:
        with pytest.raises(ValueError):
            TracingConfig(exporter="otlp")

    def test_rules_need_the_normalizer(self) -> None:
        with pytest.raises(ValueError):
            TracingConfig(model_name_rules="rules.toml")

    @pytest.mark.parametrize("bad", ["", " x", "a|b"])
    def test_run_id_components_are_validated(self, bad: str) -> None:
        with pytest.raises(ValueError):
            TracingConfig(run_id=bad)


class TestFactory:
    def test_exporter_none_gives_the_null_observer_and_the_engine_run_id(
        self, tmp_path: Path
    ) -> None:
        observer, identity = build_trial_observer(
            ObservabilityConfig(), engine_run_id="run-1", output_dir=tmp_path
        )
        assert isinstance(observer, NullTrialObserver)
        assert identity == RunIdentity(run_id="run-1", run_tag="v1")
        assert not (tmp_path / RUN_IDENTITY_FILE).exists()

    def test_no_observability_block_at_all(self) -> None:
        observer, identity = build_trial_observer(None, engine_run_id="run-1")
        assert isinstance(observer, NullTrialObserver) and identity.run_id == "run-1"

    def test_external_run_id_and_tag_win(self, tmp_path: Path) -> None:
        pytest.importorskip("opentelemetry.sdk")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/v1/traces",
                run_id="toloka-arena/v1/123/1",
                run_tag="v2",
                tags=["config:gpt6_astra"],
            )
        )
        observer, identity = build_trial_observer(
            config, engine_run_id="engine-run", output_dir=tmp_path
        )
        try:
            assert identity == RunIdentity(run_id="toloka-arena/v1/123/1", run_tag="v2")
            sidecar = json.loads((tmp_path / RUN_IDENTITY_FILE).read_text())
            assert (sidecar["run_id"], sidecar["run_tag"], sidecar["written_by"]) == (
                "toloka-arena/v1/123/1",
                "v2",
                "tolokaforge",
            )
        finally:
            observer.run_finished()

    @pytest.mark.parametrize("tag", ["demo", "model:x/y", "harness:other", "Config:stem", "a:b c"])
    def test_tags_are_validated(self, tag: str) -> None:
        with pytest.raises(TracingConfigError):
            validate_tag(tag)

    def test_good_tags_pass(self) -> None:
        assert validate_tag("config:gpt6_astra") == "config:gpt6_astra"

    def test_rules_without_normalizer_is_a_config_error(self) -> None:
        with pytest.raises(ValueError):
            TracingConfig(exporter="otlp", endpoint="http://x/v1/traces", model_name_rules="r.toml")
