"""The Langfuse plugin: model-name resolution as configuration, the receiver from the
environment, the project check and the attachment step (ADR-0047)."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from otlp_receiver import GATEWAY_PAGE, OTHER_GATEWAY_PAGE, Reply, gateway_refusal
from tolokaforge_langfuse.config import LangfuseConfig
from tolokaforge_langfuse.model_names import (
    ModelNameResolverError,
    RawModelNameResolver,
    build_model_name_resolver,
)
from tolokaforge_langfuse.plugin import resolve_endpoint
from tolokaforge_langfuse.preflight import PreflightError, merge_tag_sources, validate_tag

from tolokaforge.core.models import ObservabilityConfig, TracingConfig
from tolokaforge.observability.factory import (
    RUN_IDENTITY_FILE,
    RunIdentity,
    TracingConfigError,
    build_trial_observer,
)
from tolokaforge.observability.observer import NullTrialObserver

pytestmark = pytest.mark.unit


class TestRawResolver:
    def test_vendor_model_name_is_kept_and_tagged(self) -> None:
        identity = RawModelNameResolver().resolve("openrouter", "openai/gpt-6-astra")
        assert identity.canonical == "openai/gpt-6-astra"
        assert identity.tags == ("model:openai/gpt-6-astra",)
        assert RawModelNameResolver().rules_version == "none"

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
        assert resolver.rules_version == "test"  # rides in the native `version` field


class TestTracingConfig:
    def test_defaults_are_off_and_locked(self) -> None:
        tracing = TracingConfig()
        assert (tracing.exporter, tracing.endpoint, tracing.run_tag) == ("none", None, "v1")
        settings = LangfuseConfig()
        assert settings.model_name_normalizer == "none" and settings.model_name_rules is None
        assert tracing.tags == [] and tracing.metadata == {}

    def test_otlp_without_an_endpoint_anywhere_is_a_run_start_error(self, monkeypatch) -> None:
        # the endpoint may come from the standard OTel variables, so the config alone accepts it
        # and the factory decides (destinations amendment of ADR-0047)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)  # the switch's own source
        monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)
        config = ObservabilityConfig(tracing=TracingConfig(exporter="otlp"))
        with pytest.raises(TracingConfigError, match="requires an endpoint"):
            build_trial_observer(config, engine_run_id="run-1")

    def test_expect_project_defaults_to_none(self) -> None:
        assert LangfuseConfig().expect_project is None

    def test_rules_need_the_normalizer(self) -> None:
        with pytest.raises(ValueError):
            LangfuseConfig(model_name_rules="rules.toml")

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
        with pytest.raises(PreflightError):
            validate_tag(tag)

    def test_good_tags_pass(self) -> None:
        assert validate_tag("config:pilot_agent") == "config:pilot_agent"

    def test_rules_without_normalizer_is_a_config_error(self) -> None:
        with pytest.raises(ValueError):
            LangfuseConfig(model_name_rules="r.toml")


class TestAttachmentStep:
    def test_attach_defaults_to_all_and_none_builds_no_step(self) -> None:
        from tolokaforge_langfuse.plugin import build_attachments

        endpoint = "https://lf.example/api/public/otel/v1/traces"
        config = LangfuseConfig()
        assert config.attach == "all" and config.attach_api_base is None
        for settings in (
            LangfuseConfig(attach="none", gradings=False, projection="gradings"),
            LangfuseConfig(attach="none", projection="none"),
        ):
            assert build_attachments(settings, endpoint=endpoint) is None
        # Full projection still needs ingestion when files and gradings are disabled.
        assert (
            build_attachments(LangfuseConfig(attach="none", gradings=False), endpoint=endpoint)
            is not None
        )
        none_step = build_attachments(LangfuseConfig(attach="none"), endpoint=endpoint)
        assert none_step is not None and none_step.mode == "none"
        step = build_attachments(config, endpoint=endpoint)
        assert step is not None and step._api_base == "https://lf.example"
        assert step._mode == "all"
        explicit = build_attachments(
            LangfuseConfig(attach="core", attach_api_base="https://proxy.example/lf/"),
            endpoint=endpoint,
        )
        assert explicit._api_base == "https://proxy.example/lf" and explicit._mode == "core"

    def test_attach_values_are_validated(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LangfuseConfig(attach="everything")

    def test_secret_values_take_credential_names_only(self, monkeypatch) -> None:
        from tolokaforge_langfuse import plugin

        class _Manager:
            def list_all_keys(self):
                return [
                    "OPENROUTER_API_KEY",
                    "LANGFUSE_BASE_URL",
                    "TEST_PILOT_LANGFUSE_SECRET_KEY",
                    "HOME_DIR",
                ]

            def get_secret(self, key):
                return {
                    "OPENROUTER_API_KEY": "sk-or-v1-abc",
                    "LANGFUSE_BASE_URL": "https://lf.example",
                    "TEST_PILOT_LANGFUSE_SECRET_KEY": "sk-lf-xyz",
                    "HOME_DIR": "/Users/x",
                }[key]

        monkeypatch.setattr("tolokaforge.secrets.get_default_or_none", lambda: _Manager())
        assert sorted(plugin.secret_values()) == ["sk-lf-xyz", "sk-or-v1-abc"]

    def test_secret_values_skip_the_tracing_launchers_own_variables(self, monkeypatch) -> None:
        """A ``.env`` file may carry the launcher's session id, whose name says SESSION: it is
        written on every span by design, so it is no credential."""
        from tolokaforge_langfuse import plugin

        class _Manager:
            def list_all_keys(self):
                return ["TOLOKAFORGE_TRACING_SESSION_ID", "OPENROUTER_API_KEY"]

            def get_secret(self, key):
                return {
                    "TOLOKAFORGE_TRACING_SESSION_ID": "acme/pilot/v1/pilot_agent/123",
                    "OPENROUTER_API_KEY": "sk-or-v1-abc",
                }[key]

        monkeypatch.setattr("tolokaforge.secrets.get_default_or_none", lambda: _Manager())
        assert plugin.secret_values() == ["sk-or-v1-abc"]

    def test_secret_values_use_the_one_name_filter(self, monkeypatch) -> None:
        """The plugin's list and the process environment's follow one filter: an endpoint base
        (``_BASE``) and the shell's ``PWD`` are no credentials in either."""
        from tolokaforge_langfuse import plugin

        class _Manager:
            def list_all_keys(self):
                return ["AZURE_API_BASE", "PWD", "ACME_API_KEY"]

            def get_secret(self, key):
                return {
                    "AZURE_API_BASE": "https://x.example",
                    "PWD": "/home/x",
                    "ACME_API_KEY": "k-12345678",
                }[key]

        monkeypatch.setattr("tolokaforge.secrets.get_default_or_none", lambda: _Manager())
        assert plugin.secret_items() == {"ACME_API_KEY": "k-12345678"}


class TestTheLiveGate:
    """One gate per run: every live span, the trial-end passes and the file attachments scan
    with it. It knows the credentials the process holds, in its environment and in the
    ``SecretManager`` (a ``.env`` file never reaches ``os.environ``), and the receiver's header
    values, each by name, and the shapes."""

    MANAGED = "managed-credential-value"

    @pytest.fixture(autouse=True)
    def managed_credential(self, monkeypatch) -> None:
        from tolokaforge.secrets import DictProvider, SecretManager

        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager",
            SecretManager([DictProvider({"ACME_API_KEY": self.MANAGED})]),
        )

    def test_the_gate_knows_the_environment_the_secret_manager_and_the_headers_by_name(
        self, monkeypatch
    ) -> None:
        from tolokaforge_langfuse.plugin import live_gate

        monkeypatch.setenv("FOO_TOKEN", "environment-credential-value")
        gate = live_gate({"X-Runner-Key": "runner-key-value-1234", "Accept": "json"})

        def named(text: bytes) -> list[str]:
            return [f.describe() for f in gate.scan(text)]

        assert named(self.MANAGED.encode()) == ["known-secret-value from ACME_API_KEY"]
        assert named(b"environment-credential-value") == ["known-secret-value from FOO_TOKEN"]
        assert named(b"runner-key-value-1234") == ["known-secret-value from header X-Runner-Key"]
        assert named(b"nothing to see") == [] and named(b"json") == []

    def test_a_header_value_the_runs_tags_carry_does_not_withhold_the_run(self, caplog) -> None:
        """``X-Project: pilot-dev`` is no secret and ``project:pilot-dev`` rides on every span:
        a gate that knew the header's value would withhold the whole run."""
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from tolokaforge_langfuse.otel import OTelTrialObserver, SpanQueue
        from tolokaforge_langfuse.plugin import live_gate

        gate = live_gate({"X-Project": "pilot-dev"})
        queue = SpanQueue(InMemorySpanExporter(), max_size=10, batch_size=4, interval_s=0.05)
        with caplog.at_level("WARNING", logger="tolokaforge_langfuse.otel"):
            OTelTrialObserver(
                queue=queue, label="l", session_id="s", tags=("project:pilot-dev",), gate=gate
            ).run_finished()
        assert any("header X-Project" in r.getMessage() for r in caplog.records)
        assert gate.scan(b"project:pilot-dev") == []

    def test_a_secret_registered_after_the_gate_was_built_is_known_after_a_refresh(self) -> None:
        """``register_runtime_secret`` replaces the default manager (the engine's generated
        TypeSense key arrives that way, after the observer is built)."""
        from tolokaforge_langfuse.plugin import live_gate

        from tolokaforge.secrets import register_runtime_secret

        gate = live_gate()
        assert gate.refresh() == (False, [])  # still the manager it was built from
        register_runtime_secret("TYPESENSE_API_KEY", "late-registered-key-value")
        assert gate.refresh() == (True, [])
        assert [f.describe() for f in gate.scan(b"late-registered-key-value")] == [
            "known-secret-value from TYPESENSE_API_KEY"
        ]
        assert gate.scan(self.MANAGED.encode())  # what it held stays
        assert gate.refresh() == (False, [])

    def test_the_attachment_step_scans_with_the_runs_gate(self) -> None:
        from tolokaforge_langfuse.plugin import build_attachments
        from tolokaforge_langfuse.safety import SafetyGate

        value = "a-db-password-no-shape-matches"
        events = [{"body": {"output": f"it said {value}"}}]
        endpoint = "https://lf.example/api/public/otel/v1/traces"
        gate = SafetyGate.from_environment({"DB_PASSWORD": value})
        assert build_attachments(LangfuseConfig(), endpoint=endpoint, gate=gate).scan_events(events)
        # without the run's gate the step knows only the SecretManager's own list
        assert build_attachments(LangfuseConfig(), endpoint=endpoint).scan_events(events) == []

    def test_the_observer_the_plugin_builds_withholds_a_managed_credential(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from datetime import datetime

        from tolokaforge.core.models import ToolCall
        from tolokaforge.observability.observer import TrialIdentity
        from tolokaforge.tools.registry import ToolResult

        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/v1/traces",
                options={"langfuse": {"attach": "none"}},
            )
        )
        observer, run = build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)
        now = datetime(2026, 10, 1, tzinfo=UTC)
        trial = TrialIdentity(
            run_id=run.run_id, task_id="T-1", trial_index=0, attempt_id=0, run_tag=run.run_tag
        )
        observer.tool_call(
            trial,
            role="agent",
            index=1,
            call=ToolCall(id="c1", name="shell", arguments={}),
            result=ToolResult(success=True, output=f"the config says {self.MANAGED}"),
            started_at=now,
            ended_at=now,
        )
        receipt = observer.run_finished()
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert receipt.spans_queued == 0

    def test_the_trial_end_pass_the_plugin_builds_withholds_what_the_managers_list_never_held(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``DB_PASSWORD`` is an environment name the SecretManager's list does not hold, so the
        attachment step's own scan never knew it: the bundle projection carrying it was sent."""
        pytest.importorskip("opentelemetry.sdk")
        import parity_bundle as pb
        import yaml

        from tolokaforge.observability.observer import TrialIdentity
        from tolokaforge_langfuse import plugin

        value = "a-db-password-no-shape-matches"
        monkeypatch.setenv("DB_PASSWORD", value)
        assert value not in plugin.secret_values()
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        trajectory = pb.trajectory()
        assistant = next(m for m in trajectory["messages"] if m["role"] == "assistant")
        assistant["content"] = f"the password is {value}"
        (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory), encoding="utf-8")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/v1/traces",
                options={"langfuse": {"attach": "none"}},
            )
        )
        observer, run = build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)
        trial = TrialIdentity(
            run_id=run.run_id, task_id=pb.TASK_ID, trial_index=0, attempt_id=0, run_tag=run.run_tag
        )
        observer.trial_persisted(trial, trial_dir=trial_dir)
        receipt = observer.run_finished()
        assert receipt.extra["langfuse.projections_refused_secret"] == 1
        assert receipt.extra["langfuse.projections_sent"] == 0


class TestReceiverFromTheEnvironment:
    """A launcher (the connector's with-environment) injects the receiver; the config may stay
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
        def merge(configured, extra):
            return merge_tag_sources(("config", configured), ("launcher", extra))[0]

        assert merge(["team:pilot"], ["project:pilot-dev", "team:pilot"]) == [
            "team:pilot",
            "project:pilot-dev",
        ]
        with pytest.raises(PreflightError, match="given twice"):
            merge(["project:pilot"], ["project:pilot-dev"])
        with pytest.raises(PreflightError):
            merge([], ["model:x/y"])  # reserved prefixes stay reserved for injected tags

    def _projects(
        self, monkeypatch, answer, family_answer=(404, b"")
    ) -> list[tuple[str, str, dict]]:
        from tolokaforge_langfuse import media

        calls: list[tuple[str, str, dict]] = []

        def opener(method, url, headers, body, timeout):
            calls.append((method, url, dict(headers)))
            if media.V2_OBSERVATIONS_PATH in url:
                # the receiver-family probe; a v3 server 404s it
                return family_answer
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(media, "urllib_opener", opener)
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
        config = ObservabilityConfig(
            tracing=TracingConfig(exporter="otlp", options={"langfuse": {"attach": "none"}})
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)
        try:
            assert [(m, u) for m, u, _ in calls] == [
                ("GET", "http://127.0.0.1:9/api/public/projects"),
                ("GET", "http://127.0.0.1:9/api/public/v2/observations?limit=1"),
            ]
            # the check authenticates with the exporter's own header
            assert calls[0][2]["Authorization"] == "Basic dGVzdDpzZWNyZXQ="
            assert observer._tags == ("project:pilot-dev",)
        finally:
            receipt = observer.run_finished()
        assert (
            receipt.details[0]["expect_project"] == "pilot-dev"
            and receipt.details[0]["project_verified"] == "verified"
            and receipt.details[0]["server_api"] == "v3"
        )
        assert receipt.model_dump(mode="json")["details"][0]["project_verified"] == "verified"

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
                options={"langfuse": {"expect_project": "pilot", "attach": "none"}},
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
                options={"langfuse": {"expect_project": "pilot", "attach": "none"}},
            )
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        receipt = observer.run_finished()
        assert (
            receipt.details[0]["project_verified"] == "unverified"
            and receipt.details[0]["expect_project"] == "pilot"
        )

    def test_a_401_refuses_and_a_non_json_200_is_unverified(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                options={"langfuse": {"expect_project": "pilot-dev", "attach": "none"}},
            )
        )
        self._projects(monkeypatch, (401, b'{"message":"Unauthorized"}'))
        with pytest.raises(TracingConfigError, match="HTTP 401.*open no project"):
            build_trial_observer(config, engine_run_id="run-1")
        self._projects(monkeypatch, (200, b"<html>not json</html>"))
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        assert observer.run_finished().details[0]["project_verified"] == "unverified"

    def test_expect_project_without_any_headers_refuses(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        self._projects(monkeypatch, (200, b"{}"))
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
        from tolokaforge.secrets import DictProvider, SecretManager

        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager", SecretManager([DictProvider({})])
        )
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                options={"langfuse": {"expect_project": "pilot-dev", "attach": "none"}},
            )
        )
        with pytest.raises(TracingConfigError, match="needs the receiver credentials"):
            build_trial_observer(config, engine_run_id="run-1")

    def test_api_base_drops_userinfo(self) -> None:
        from tolokaforge_langfuse.media import api_base_from_endpoint

        assert (
            api_base_from_endpoint("https://pk:sk@lf.example:8443/api/public/otel/v1/traces")
            == "https://lf.example:8443"
        )

    def test_without_expect_project_only_the_family_is_probed(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        calls = self._projects(monkeypatch, (200, b"{}"))
        monkeypatch.delenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", raising=False)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_TAGS", raising=False)
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/v1/traces",
                options={"langfuse": {"attach": "none"}},
            )
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        receipt = observer.run_finished()
        # the project is not checked; the family probe is read-only and always runs under auto
        assert [u for _, u, _ in calls] == ["http://127.0.0.1:9/api/public/v2/observations?limit=1"]
        assert receipt.details[0]["project_verified"] == "none"


class TestTheReceiverFamily:
    """By capability, once per run, never by the version the receiver reports."""

    def _build(
        self,
        monkeypatch,
        family_answer,
        options=None,
        endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
    ):
        from tolokaforge_langfuse import media

        calls: list[str] = []

        def opener(method, url, headers, body, timeout):
            calls.append(url)
            if media.V2_OBSERVATIONS_PATH in url:
                if isinstance(family_answer, Exception):
                    raise family_answer
                return family_answer
            return (200, b'{"data": []}')

        monkeypatch.setattr(media, "urllib_opener", opener)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_TAGS", raising=False)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic dGVzdDpzZWNyZXQ=")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint=endpoint,
                options={"langfuse": {"attach": "none", **(options or {})}},
            )
        )
        return build_trial_observer(config, engine_run_id="run-1")[0], calls

    @pytest.mark.parametrize(
        ("answer", "family"),
        [((200, b'{"data": []}'), "v4"), ((404, b""), "v3"), ((500, b""), "v3")],
    )
    def test_the_probe_decides_the_family(self, monkeypatch, answer, family) -> None:
        pytest.importorskip("opentelemetry.sdk")
        observer, calls = self._build(monkeypatch, answer)
        assert observer._write_once is (family == "v4")
        assert observer.run_finished().details[0]["server_api"] == family

    def test_an_unreachable_receiver_leaves_the_run_on_the_v3_family(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        observer, _ = self._build(monkeypatch, OSError("connection refused"))
        assert observer.run_finished().details[0]["server_api"] == "v3"

    def test_the_override_skips_the_probe(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        observer, calls = self._build(monkeypatch, (404, b""), options={"server_api": "v4"})
        assert observer._write_once
        assert not [u for u in calls if "/v2/observations" in u]
        assert observer.run_finished().details[0]["server_api"] == "v4"

    def test_the_v3_family_is_written_exactly_as_before(self, monkeypatch) -> None:
        """The direct ingestion path and the single-post exporter are v4 answers: a v3 receiver
        gets neither, so its wire traffic is the one this observer has always written."""
        pytest.importorskip("opentelemetry.sdk")
        from otlp_receiver import Receiver
        from tolokaforge_langfuse.otlp_transport import INGESTION_VERSION_HEADER

        with Receiver() as receiver:
            observer, _ = self._build(monkeypatch, (404, b""), endpoint=receiver.url())
            exporter = observer._queue._exporter
            assert type(exporter).__name__ == "OTLPSpanExporter"
            exporter.export([])
        [post] = receiver.posts
        assert INGESTION_VERSION_HEADER not in post.headers

    def test_the_v4_family_asks_for_the_direct_path_and_posts_once(self, monkeypatch) -> None:
        """One POST for an answer the retry policy does not name: a 502 may come after the
        receiver took the body."""
        pytest.importorskip("opentelemetry.sdk")
        from otlp_receiver import Receiver
        from tolokaforge_langfuse.otlp_transport import INGESTION_VERSION_HEADER

        with Receiver() as receiver:
            observer, _ = self._build(monkeypatch, (200, b'{"data": []}'), endpoint=receiver.url())
            exporter = observer._queue._exporter
            assert type(exporter).__name__ == "SingleAttemptSpanExporter"
            receiver.answer = 502
            exporter.export([])
        [post] = receiver.posts
        assert post.headers[INGESTION_VERSION_HEADER] == "4"

    def test_a_page_that_is_not_this_api_is_not_a_v4_receiver(self, monkeypatch) -> None:
        """An authenticating proxy answers 200 with an HTML login page on any path; taking that
        for a receiver would put the whole run on the write-once layout against a v3 one."""
        pytest.importorskip("opentelemetry.sdk")
        observer, _ = self._build(monkeypatch, (200, b"<html><body>Sign in</body></html>"))
        assert observer.run_finished().details[0]["server_api"] == "v3"

    def test_a_v4_run_stops_when_the_sdk_cannot_post_once(self, monkeypatch) -> None:
        """The v4 producer's single-attempt policy is required at run start."""
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge_langfuse import otlp_transport

        monkeypatch.setattr(otlp_transport, "_single_attempt_exporter_class", lambda: None)
        with pytest.raises(TracingConfigError, match="requires a single attempt"):
            self._build(monkeypatch, (200, b'{"data": []}'))

    def test_the_same_sdk_leaves_a_v3_run_alone(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge_langfuse import otlp_transport

        monkeypatch.setattr(otlp_transport, "_single_attempt_exporter_class", lambda: None)
        observer, _ = self._build(monkeypatch, (404, b""))
        assert observer.run_finished().details[0]["server_api"] == "v3"

    def test_a_write_once_receiver_needs_the_full_projection(self, monkeypatch) -> None:
        pytest.importorskip("opentelemetry.sdk")
        with pytest.raises(TracingConfigError, match="projection='gradings' cannot be used"):
            self._build(
                monkeypatch,
                (200, b'{"data": []}'),
                options={"projection": "gradings", "attach": "all"},
            )


class TestTheRetryPolicy:
    """``options.langfuse.retry`` reaches every write route of the run under one count."""

    def _build(
        self, monkeypatch, family_answer, options=None, endpoint=None, writes=None, tracing=None
    ):
        from tolokaforge_langfuse import media

        def opener(method, url, headers, body, timeout):
            if media.V2_OBSERVATIONS_PATH in url:
                return family_answer
            if writes is not None:
                return writes(method, url, headers, body, timeout)
            return (200, b'{"data": []}')

        monkeypatch.setattr(media, "urllib_opener", opener)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_TAGS", raising=False)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_EXPECT_PROJECT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic dGVzdDpzZWNyZXQ=")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint=endpoint or "http://127.0.0.1:9/api/public/otel/v1/traces",
                options={"langfuse": options or {}},
                # the worker stays asleep: the run's end exports the spans, after the trial ends
                **{"export_interval_s": 3600, **(tracing or {})},
            )
        )
        return build_trial_observer(config, engine_run_id="run-1")[0]

    def _live_run(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        family=(200, b'{"data": []}'),
        retry: dict | None = None,
        tracing: dict | None = None,
        span_answers=(),
        span_default=None,
        ingestion_answers=(),
        before_ingestion=None,
    ):
        """One trial through the plugin, then the run's end. The spans go to a local receiver
        that answers ``span_answers`` first, then ``span_default`` (200 unless given); the
        ingestion route calls ``before_ingestion``, then answers ``ingestion_answers`` first and
        accepts after them. The retries and the run-end flush share one fake time. Returns the
        receipt, the span posts, the ingestion bodies and the fake time."""
        import functools
        from datetime import datetime, timedelta

        import parity_bundle as pb
        from fake_time import FakeTime
        from otlp_receiver import Receiver
        from tolokaforge_langfuse.retry import Retrier

        from tolokaforge.observability.observer import TrialIdentity
        from tolokaforge_langfuse import media, otel, plugin

        time = FakeTime()
        monkeypatch.setattr(
            plugin,
            "Retrier",
            functools.partial(Retrier, clock=time.clock, sleep=time.sleep, draw=lambda: 0.0),
        )
        # the trial's budget sets the deadline its retrier reads, so both run on one clock;
        # on the real one the deadline would depend on how long the machine has been up
        monkeypatch.setattr(
            media,
            "LangfuseAttachments",
            functools.partial(media.LangfuseAttachments, clock=time.clock),
        )
        monkeypatch.setattr(
            otel, "SpanQueue", functools.partial(otel.SpanQueue, clock=time.clock, sleep=time.sleep)
        )
        answers = list(ingestion_answers)
        ingestion: list[bytes] = []

        def writes(method, url, headers, body, timeout):
            if url.endswith("/api/public/ingestion"):
                if before_ingestion is not None:
                    before_ingestion()
                ingestion.append(body)
                return answers.pop(0) if answers else (207, b'{"successes": [], "errors": []}')
            return (404, b"")

        started = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)

        class _Done:
            status = "completed"
            start_ts = started
            end_ts = started + timedelta(seconds=30)

        identity = TrialIdentity(
            run_id=pb.RUN_ID,
            task_id=pb.TASK_ID,
            trial_index=pb.TRIAL_INDEX,
            attempt_id=pb.ATTEMPT_ID,
            run_tag=pb.RUN_TAG,
        )
        options: dict = {"attach": "none"}
        if retry is not None:
            options["retry"] = retry
        with Receiver() as receiver:
            receiver.script = list(span_answers)
            if span_default is not None:
                receiver.answer = span_default
            observer = self._build(
                monkeypatch,
                family,
                options=options,
                endpoint=receiver.url(),
                writes=writes,
                tracing=tracing,
            )
            observer.trial_started(identity, models={}, started_at=started)
            observer.trial_finished(identity, trajectory=_Done())
            observer.trial_persisted(identity, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
            receipt = observer.run_finished()
        return receipt, receiver.posts, ingestion, time

    @pytest.mark.parametrize(
        ("retry", "page", "wait"),
        [
            (None, GATEWAY_PAGE, 1.0),
            ({"gateway_markers": ["example-gateway"], "delays_s": [2]}, OTHER_GATEWAY_PAGE, 2.0),
        ],
        ids=["default-policy", "configured-policy"],
    )
    def test_a_live_run_delivers_what_the_gateway_refused_first(
        self, monkeypatch, tmp_path: Path, retry, page: bytes, wait: float
    ) -> None:
        """End to end on a v4 receiver: the batch of final spans and the score batch each meet
        the gateway's refusal page once, are posted again after the schedule's first wait, and
        land; the receipt says so. The run's ``retry`` block reaches both routes: which page is
        the gateway's and how long the wait is. Real posts to a local receiver, fake time."""
        pytest.importorskip("opentelemetry.sdk")
        receipt, posts, ingestion, time = self._live_run(
            monkeypatch,
            tmp_path,
            retry=retry,
            span_answers=[Reply(403, page, (("Content-Type", "text/html"),))],
            ingestion_answers=[(403, page)],
        )
        first, second = posts
        assert first.body == second.body
        assert len(ingestion) == 2 and ingestion[0] == ingestion[1]
        extra = receipt.extra
        assert receipt.export_failures == 0 and receipt.spans_dropped == 0
        assert receipt.spans_exported == extra["langfuse.final_observations_sent"] > 0
        assert extra["langfuse.gradings_sent"] == 1 and extra["langfuse.gradings_failed"] == 0
        assert extra["langfuse.retried_requests"] == 2
        assert extra["langfuse.retries_recovered"] == 2
        assert extra["langfuse.retries_exhausted"] == 0
        assert extra["langfuse.retry_wait_s"] == 2 * wait
        assert time.sleeps == [wait, wait]

    def test_the_trial_ends_obey_the_breaker_without_opening_it(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A score batch that runs out its whole schedule does not open the breaker, even with
        ``breaker_after: 1``: the span export after it still waits its refusal out."""
        pytest.importorskip("opentelemetry.sdk")
        receipt, posts, ingestion, time = self._live_run(
            monkeypatch,
            tmp_path,
            retry={"max_retries": 1, "breaker_after": 1},
            span_answers=[gateway_refusal()],
            ingestion_answers=[(403, GATEWAY_PAGE)] * 2,
        )
        assert len(ingestion) == 2 and receipt.extra["langfuse.gradings_failed"] == 1
        first, second = posts
        assert first.body == second.body and receipt.export_failures == 0
        assert receipt.extra["langfuse.retry_breaker_trips"] == 0
        assert time.sleeps == [1.0, 1.0]

    def test_the_breaker_the_span_export_opens_stops_the_trial_ends(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """One breaker for the run's routes: the span export runs out its schedule against a
        gateway that keeps refusing (``max_retries: 1``, ``breaker_after: 1``), and the score
        batch the gateway refuses after that is not waited out. The background worker posts
        each span as it is queued; the score batch is answered once the breaker has opened."""
        pytest.importorskip("opentelemetry.sdk")
        import logging
        import threading

        opened = threading.Event()

        class _BreakerWatch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if "refusals now fail at once" in record.getMessage():
                    opened.set()

        watch = _BreakerWatch()
        retry_log = logging.getLogger("tolokaforge_langfuse.retry")
        retry_log.addHandler(watch)
        try:
            receipt, _, ingestion, _ = self._live_run(
                monkeypatch,
                tmp_path,
                retry={"max_retries": 1, "breaker_after": 1},
                tracing={"export_batch_size": 1},
                span_default=gateway_refusal(),
                ingestion_answers=[(403, GATEWAY_PAGE)],
                before_ingestion=lambda: opened.wait(10),
            )
        finally:
            retry_log.removeHandler(watch)
        assert opened.is_set()
        assert len(ingestion) == 1 and receipt.extra["langfuse.gradings_failed"] == 1
        assert receipt.extra["langfuse.retry_breaker_trips"] == 1

    def test_the_run_end_waits_within_the_configured_grace(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``flush_grace_s: 0``: a span batch the gateway refuses at the run's end is not waited
        out past ``flush_timeout_s`` (30 s), and the receipt counts it as not delivered."""
        pytest.importorskip("opentelemetry.sdk")
        receipt, posts, _, time = self._live_run(
            monkeypatch,
            tmp_path,
            retry={"delays_s": [45], "flush_grace_s": 0},
            span_answers=[gateway_refusal()],
        )
        assert time.sleeps == []
        assert receipt.export_failures >= 1 and receipt.extra["langfuse.retries_exhausted"] >= 1
        assert len({post.body for post in posts}) == len(posts)  # nothing was posted twice

    def test_a_v3_run_retries_its_trial_end_calls_and_leaves_its_spans_to_the_sdk(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """On a v3 receiver the trial-end calls follow the run's policy, while the spans keep
        the SDK's exporter, which does not post the gateway's page again."""
        pytest.importorskip("opentelemetry.sdk")
        _, posts, ingestion, time = self._live_run(
            monkeypatch,
            tmp_path,
            family=(404, b""),
            retry={"delays_s": [2]},
            span_answers=[gateway_refusal()],
            ingestion_answers=[(403, GATEWAY_PAGE)],
        )
        assert len(ingestion) == 2 and ingestion[0] == ingestion[1]
        assert time.sleeps == [2.0]
        assert posts and len({post.body for post in posts}) == len(posts)


class TestSecretManagerBoundary:
    def test_headers_and_keys_do_not_bypass_an_empty_manager(self, monkeypatch):
        from tolokaforge_langfuse.plugin import langfuse_headers, otlp_headers

        from tolokaforge.secrets import DictProvider, SecretManager

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=external")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "external-public")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "external-secret")
        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager", SecretManager([DictProvider({})])
        )
        assert otlp_headers() is None
        assert langfuse_headers() is None

    def test_headers_follow_the_configured_provider_chain(self, monkeypatch):
        from tolokaforge_langfuse.plugin import otlp_headers

        from tolokaforge.secrets import DictProvider, SecretManager

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=external")
        manager = SecretManager(
            [DictProvider({"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=managed"})]
        )
        monkeypatch.setattr("tolokaforge.secrets.manager._default_manager", manager)
        assert otlp_headers() == {"Authorization": "managed"}

    def test_gateway_headers_follow_the_configured_provider_chain(self, monkeypatch):
        from tolokaforge_langfuse.plugin import langfuse_headers, receiver_headers

        from tolokaforge.secrets import DictProvider, SecretManager

        monkeypatch.setenv("LANGFUSE_EXTRA_HEADERS", "X-Gateway-Key=external")
        credentials = {
            "LANGFUSE_PUBLIC_KEY": "public",
            "LANGFUSE_SECRET_KEY": "secret",
            "LANGFUSE_EXTRA_HEADERS": "X-Gateway-Key=managed",
        }
        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager",
            SecretManager([DictProvider(credentials)]),
        )
        assert langfuse_headers()["X-Gateway-Key"] == "managed"
        assert receiver_headers()["X-Gateway-Key"] == "managed"
        credentials["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=managed-otlp"
        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager",
            SecretManager([DictProvider(credentials)]),
        )
        assert receiver_headers() == {
            "Authorization": "managed-otlp",
            "X-Gateway-Key": "managed",
        }


class TestPluginOptions:
    @pytest.mark.parametrize(
        "options",
        [
            {"attach": "everything"},
            {"expect_projct": "pilot"},
            {"attach_timeout_s": 0},
            {"attach_budget_s": -1},
            {"model_name_rules": "rules.toml"},
            {"retry": {"statuses": [200]}},
            "not a mapping",
            None,
        ],
    )
    def test_invalid_options_fail_before_endpoint_resolution(self, monkeypatch, options):
        from tolokaforge_langfuse.plugin import build

        monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)
        config = TracingConfig(exporter="otlp", options={"langfuse": options})
        with pytest.raises(TracingConfigError, match=r"observability.tracing.options.langfuse"):
            build(config, RunIdentity("run-1"), engine_run_id="run-1")

    def test_another_plugins_options_are_opaque(self):
        # the reader the live path runs (build -> plan_run -> resolve_plan -> read_settings)
        from tolokaforge_langfuse.preflight import read_settings

        assert read_settings({"archive": {"compression": "gzip"}}) == LangfuseConfig()
        assert read_settings({"langfuse": {"attach": "core"}, "archive": None}).attach == "core"

    def test_unselected_plugin_does_not_validate_options(self, monkeypatch):
        from tolokaforge_langfuse.plugin import build

        monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)
        config = TracingConfig(exporter="archive", options={"archive": {"format": "json"}})
        assert build(config, RunIdentity("run-1"), engine_run_id="run-1") is None


@pytest.mark.parametrize("offered", [1, 2, 3, 5, None])
def test_incompatible_plugin_contract_is_rejected_at_start(monkeypatch, offered):
    from tolokaforge_langfuse.plugin import check_engine_api

    from tolokaforge.observability import factory

    monkeypatch.setattr(factory, "PLUGIN_API_VERSION", offered)
    with pytest.raises(TracingConfigError, match="speaks trial-observer API v4"):
        check_engine_api()
