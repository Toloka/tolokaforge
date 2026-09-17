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
    merge_tags,
    resolve_endpoint,
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

    def test_otlp_without_an_endpoint_anywhere_is_a_run_start_error(self, monkeypatch) -> None:
        # the endpoint may come from the standard OTel variables, so the config alone accepts it
        # and the factory decides (destinations amendment of ADR-0046)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)  # the switch's own source
        monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)
        config = ObservabilityConfig(tracing=TracingConfig(exporter="otlp"))
        with pytest.raises(TracingConfigError, match="requires an endpoint"):
            build_trial_observer(config, engine_run_id="run-1")

    def test_expect_project_defaults_to_none(self) -> None:
        assert TracingConfig().expect_project is None

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
                run_id="acme/pilot/v1/123/1",
                run_tag="v2",
                tags=["config:pilot_agent"],
            )
        )
        observer, identity = build_trial_observer(
            config, engine_run_id="engine-run", output_dir=tmp_path
        )
        try:
            assert identity == RunIdentity(run_id="acme/pilot/v1/123/1", run_tag="v2")
            sidecar = json.loads((tmp_path / RUN_IDENTITY_FILE).read_text())
            assert (sidecar["run_id"], sidecar["run_tag"], sidecar["written_by"]) == (
                "acme/pilot/v1/123/1",
                "v2",
                "tolokaforge",
            )
        finally:
            observer.run_finished()

    @pytest.mark.parametrize(
        "tag", ["demo", "model:x/y", "harness:other", "task:T-1", "Config:stem", "a:b c"]
    )
    def test_tags_are_validated(self, tag: str) -> None:
        with pytest.raises(TracingConfigError):
            validate_tag(tag)

    def test_good_tags_pass(self) -> None:
        assert validate_tag("config:pilot_agent") == "config:pilot_agent"

    def test_rules_without_normalizer_is_a_config_error(self) -> None:
        with pytest.raises(ValueError):
            TracingConfig(exporter="otlp", endpoint="http://x/v1/traces", model_name_rules="r.toml")


class TestAttachmentStep:
    def test_attach_defaults_to_all_and_none_builds_no_step(self) -> None:
        from tolokaforge.core.models import TracingConfig
        from tolokaforge.observability.factory import build_attachments

        config = TracingConfig(
            exporter="otlp", endpoint="https://lf.example/api/public/otel/v1/traces"
        )
        assert config.attach == "all" and config.attach_api_base is None
        # nothing to send at trial end (no files, the gradings-only projection without gradings):
        # no receiver-side step at all; the default full projection, or gradings alone, still
        # need the ingestion route, so attach: none by itself builds a step in mode none
        assert (
            build_attachments(
                TracingConfig(exporter="none", attach="none", gradings=False, projection="gradings")
            )
            is None
        )
        assert (
            build_attachments(TracingConfig(exporter="none", attach="none", projection="none"))
            is None
        )
        assert (
            build_attachments(
                TracingConfig(
                    exporter="none",
                    endpoint="https://lf.example/api/public/otel/v1/traces",
                    attach="none",
                    gradings=False,
                )
            )
            is not None
        )
        none_step = build_attachments(
            TracingConfig(
                exporter="otlp",
                endpoint="https://lf.example/api/public/otel/v1/traces",
                attach="none",
            )
        )
        assert none_step is not None and none_step.mode == "none"
        pytest.importorskip("opentelemetry.sdk")
        step = build_attachments(config)
        assert step is not None and step._api_base == "https://lf.example"
        assert step._mode == "all"
        explicit = build_attachments(
            TracingConfig(
                exporter="otlp",
                endpoint="https://lf.example/api/public/otel/v1/traces",
                attach="core",
                attach_api_base="https://proxy.example/lf/",
            )
        )
        assert explicit._api_base == "https://proxy.example/lf" and explicit._mode == "core"

    def test_attach_values_are_validated(self) -> None:
        from pydantic import ValidationError

        from tolokaforge.core.models import TracingConfig

        with pytest.raises(ValidationError):
            TracingConfig(exporter="none", attach="everything")

    def test_secret_values_take_credential_names_only(self, monkeypatch) -> None:
        from tolokaforge.observability import factory

        class _Manager:
            def list_all_keys(self):
                return [
                    "OPENROUTER_API_KEY",
                    "LANGFUSE_BASE_URL",
                    "TEST_ARENA_LANGFUSE_SECRET_KEY",
                    "HOME_DIR",
                ]

            def get_secret(self, key):
                return {
                    "OPENROUTER_API_KEY": "sk-or-v1-abc",
                    "LANGFUSE_BASE_URL": "https://lf.example",
                    "TEST_ARENA_LANGFUSE_SECRET_KEY": "sk-lf-xyz",
                    "HOME_DIR": "/Users/x",
                }[key]

        monkeypatch.setattr("tolokaforge.secrets.get_default_or_none", lambda: _Manager())
        assert sorted(factory.secret_values()) == ["sk-lf-xyz", "sk-or-v1-abc"]


class TestReceiverFromTheEnvironment:
    """A launcher (the connector's with-destination) injects the receiver; the config may stay
    vendor-neutral and endpoint-free."""

    def test_endpoint_resolution_order(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://lf.example/api/public/otel/")
        assert resolve_endpoint(None) == "https://lf.example/api/public/otel/v1/traces"
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://lf.example/api/public/otel/v1/traces"
        )
        assert resolve_endpoint(None) == "https://lf.example/api/public/otel/v1/traces"
        assert (
            resolve_endpoint("https://other.example/v1/traces") == "https://other.example/v1/traces"
        )

    def test_environment_tags_merge_and_a_prefix_never_carries_two_values(self) -> None:
        assert merge_tags(["team:pilot"], ["project:pilot-dev", "team:pilot"]) == [
            "team:pilot",
            "project:pilot-dev",
        ]
        with pytest.raises(TracingConfigError, match="given twice"):
            merge_tags(["project:pilot"], ["project:pilot-dev"])
        with pytest.raises(TracingConfigError):
            merge_tags([], ["model:x/y"])  # reserved prefixes stay reserved for injected tags

    def _projects(self, monkeypatch, answer) -> list[tuple[str, str, dict]]:
        from tolokaforge.observability import langfuse_media

        calls: list[tuple[str, str, dict]] = []

        def opener(method, url, headers, body, timeout):
            calls.append((method, url, dict(headers)))
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(langfuse_media, "urllib_opener", opener)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_TAGS", raising=False)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic dGVzdDpzZWNyZXQ=")
        return calls

    def test_expect_project_verified_lands_in_the_receipt(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("opentelemetry.sdk")
        calls = self._projects(
            monkeypatch, (200, json.dumps({"data": [{"id": "p1", "name": "pilot-dev"}]}).encode())
        )
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:9/api/public/otel/v1/traces"
        )
        monkeypatch.setenv("TOLOKAFORGE_TRACING_TAGS", "project:pilot-dev")
        monkeypatch.setenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", "pilot-dev")
        config = ObservabilityConfig(tracing=TracingConfig(exporter="otlp", attach="none"))
        observer, _ = build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)
        try:
            assert [(m, u) for m, u, _ in calls] == [
                ("GET", "http://127.0.0.1:9/api/public/projects")
            ]
            # the check authenticates with the exporter's own header
            assert calls[0][2]["Authorization"] == "Basic dGVzdDpzZWNyZXQ="
            assert observer._tags == ("project:pilot-dev",)
        finally:
            receipt = observer.run_finished()
        assert receipt.expect_project == "pilot-dev" and receipt.project_verified == "verified"
        assert receipt.to_dict()["project_verified"] == "verified"

    def test_expect_project_mismatch_refuses_to_trace_before_anything_starts(
        self, monkeypatch
    ) -> None:
        pytest.importorskip("opentelemetry.sdk")
        self._projects(
            monkeypatch, (200, json.dumps({"data": [{"id": "p2", "name": "pilot-dev"}]}).encode())
        )
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                expect_project="pilot",
                attach="none",
            )
        )
        with pytest.raises(TracingConfigError, match="expect_project='pilot'.*\\['pilot-dev'\\]"):
            build_trial_observer(config, engine_run_id="run-1")

    def test_unreachable_check_is_unverified_not_fatal(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        self._projects(monkeypatch, (403, b"forbidden"))
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                expect_project="pilot",
                attach="none",
            )
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        receipt = observer.run_finished()
        assert receipt.project_verified == "unverified" and receipt.expect_project == "pilot"

    def test_a_401_refuses_and_a_non_json_200_is_unverified(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                expect_project="pilot-dev",
                attach="none",
            )
        )
        self._projects(monkeypatch, (401, b'{"message":"Unauthorized"}'))
        with pytest.raises(TracingConfigError, match="HTTP 401.*open no project"):
            build_trial_observer(config, engine_run_id="run-1")
        self._projects(monkeypatch, (200, b"<html>not json</html>"))
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        assert observer.run_finished().project_verified == "unverified"

    def test_expect_project_without_any_headers_refuses(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        self._projects(monkeypatch, (200, b"{}"))
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
        monkeypatch.setattr("tolokaforge.secrets.get_default_or_none", lambda: None)
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                expect_project="pilot-dev",
                attach="none",
            )
        )
        with pytest.raises(TracingConfigError, match="needs the receiver credentials"):
            build_trial_observer(config, engine_run_id="run-1")

    def test_api_base_drops_userinfo(self) -> None:
        from tolokaforge.observability.langfuse_media import api_base_from_endpoint

        assert (
            api_base_from_endpoint("https://pk:sk@lf.example:8443/api/public/otel/v1/traces")
            == "https://lf.example:8443"
        )

    def test_without_expect_project_nothing_is_checked(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        calls = self._projects(monkeypatch, (200, b"{}"))
        monkeypatch.delenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", raising=False)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_TAGS", raising=False)
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp", endpoint="http://127.0.0.1:9/v1/traces", attach="none"
            )
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        receipt = observer.run_finished()
        assert calls == [] and receipt.project_verified == "none"
