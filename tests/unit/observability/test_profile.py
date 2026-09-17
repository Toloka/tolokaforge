"""The deployment profile (ADR-0047, parity amendment): loading and validation without any
deployment value in the engine, the environment rule and its overrides, the per-run metadata
variable and the clash check, the native fields the factory derives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.models import ObservabilityConfig, TracingConfig
from tolokaforge.observability.factory import (
    RUN_IDENTITY_FILE,
    TracingConfigError,
    build_trial_observer,
    merge_metadata,
    merge_tag_sources,
    producer_version,
    resolve_environment,
)
from tolokaforge.observability.profile import (
    NO_PROFILE,
    TracingProfileError,
    load_tracing_profile,
    main,
    parse_metadata_variable,
    profile_from_mapping,
)

pytestmark = pytest.mark.unit

PROFILE = """
schema = 1
version = "acme-2026.09.17.1"

[environment]
from_tag = "run_kind"
default = "development"
[environment.values]
eval = "production"

[tags]
fixed = ["team:pilot"]

[metadata.fixed]
deployment = "pilot"
"""


def write_profile(tmp_path: Path, text: str = PROFILE, name: str = "profile.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    def test_a_full_profile_loads(self, tmp_path: Path) -> None:
        profile = load_tracing_profile(write_profile(tmp_path))
        assert profile.version == "acme-2026.09.17.1"
        assert profile.environment.from_tag == "run_kind"
        assert profile.environment.values == {"eval": "production"}
        assert profile.environment.default == "development"
        assert profile.fixed_tags == ("team:pilot",)
        assert profile.fixed_metadata == {"deployment": "pilot"}
        assert profile.model_name_rules is None

    def test_a_bare_profile_leaves_the_environment_to_the_receiver(self) -> None:
        profile = profile_from_mapping({"schema": 1, "version": "p-1"})
        assert profile.version == "p-1" and profile.environment.resolve([]) is None

    def test_a_literal_environment(self) -> None:
        profile = profile_from_mapping(
            {"schema": 1, "version": "p", "environment": {"literal": "staging"}}
        )
        assert profile.environment.resolve(["run_kind:eval"]) == "staging"

    def test_the_rule_maps_the_tag_value_and_falls_back_to_the_default(
        self, tmp_path: Path
    ) -> None:
        rule = load_tracing_profile(write_profile(tmp_path)).environment
        assert rule.resolve(["run_kind:eval", "scope:full"]) == "production"
        assert rule.resolve(["run_kind:smoke"]) == "development"
        assert rule.resolve(["scope:full"]) == "development"

    def test_model_rules_resolve_next_to_the_profile_and_must_exist(self, tmp_path: Path) -> None:
        (tmp_path / "rules.toml").write_text('schema_version = 1\nversion = "r"\n')
        profile = load_tracing_profile(
            write_profile(tmp_path, 'schema = 1\nversion = "p"\n[models]\nrules = "rules.toml"\n')
        )
        assert profile.model_name_rules == str(tmp_path / "rules.toml")
        with pytest.raises(TracingProfileError, match="no such file"):
            load_tracing_profile(
                write_profile(
                    tmp_path, 'schema = 1\nversion = "p"\n[models]\nrules = "gone.toml"\n'
                )
            )

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("version = 'p'\n", "'schema' must be 1"),
            ("schema = 1\n", "'version' must be"),
            ("schema = 1\nversion = 'a+b'\n", "without '\\+'"),
            ("schema = 1\nversion = 'p'\nextra = 1\n", "unknown top-level keys"),
            (
                "schema = 1\nversion = 'p'\n[environment]\nliteral = 'x'\nfrom_tag = 'y'\n",
                "exactly one of",
            ),
            (
                "schema = 1\nversion = 'p'\n[environment]\nfrom_tag = 'run_kind'\n",
                "needs a 'default'",
            ),
            (
                "schema = 1\nversion = 'p'\n[environment]\nfrom_tag = 'run_kind'\ndefault = 'dev'\n",
                "non-empty table",
            ),
            (
                "schema = 1\nversion = 'p'\n[environment]\nliteral = 'Prod'\n",
                "not a valid environment",
            ),
            (
                "schema = 1\nversion = 'p'\n[environment]\nliteral = 'langfuse-x'\n",
                "not a valid environment",
            ),
            ("schema = 1\nversion = 'p'\n[tags]\nfixed = ['harness:x']\n", "set by the exporter"),
            ("schema = 1\nversion = 'p'\n[tags]\nfixed = ['team:a', 'team:b']\n", "two values"),
            ("schema = 1\nversion = 'p'\n[tags]\nfixed = ['nocolon']\n", "must look like"),
            (
                "schema = 1\nversion = 'p'\n[tags]\nmirror_to_metadata = ['team']\n",
                "unknown keys",
            ),
            ("schema = 1\nversion = 'p'\n[metadata.fixed]\nk = [1]\n", "string, number or boolean"),
            ("schema = 1\nversion = 'p'\n[models]\nrules = 3\n", "non-empty path"),
            ("not toml [[[", "not valid TOML"),
        ],
    )
    def test_invalid_profiles_are_refused_by_key(
        self, tmp_path: Path, text: str, match: str
    ) -> None:
        with pytest.raises(TracingProfileError, match=match):
            load_tracing_profile(write_profile(tmp_path, text))

    def test_the_module_entry_validates_a_file(self, tmp_path: Path, capsys) -> None:
        assert main([str(write_profile(tmp_path))]) == 0
        assert "OK" in capsys.readouterr().out
        assert main([str(write_profile(tmp_path, "schema = 2\n", "bad.toml"))]) == 2
        assert "INVALID" in capsys.readouterr().err
        assert main([]) == 2


class TestMetadataVariable:
    def test_pairs_parse_and_bad_items_are_refused(self) -> None:
        assert parse_metadata_variable("a=1, b = two,,c=x=y") == {"a": "1", "b": "two", "c": "x=y"}
        assert parse_metadata_variable(None) == {} and parse_metadata_variable(" ") == {}
        with pytest.raises(TracingProfileError, match="key=value"):
            parse_metadata_variable("novalue")

    def test_merge_order_and_the_clash_check(self, monkeypatch, tmp_path: Path) -> None:
        profile = load_tracing_profile(write_profile(tmp_path))
        monkeypatch.setenv("TOLOKAFORGE_TRACING_METADATA", "deployment=launcher,run_label=x")
        merged = merge_metadata({"deployment": "config", "stem": "s"}, profile)
        # profile < config < launcher
        assert merged == {"deployment": "launcher", "stem": "s", "run_label": "x"}
        monkeypatch.setenv("TOLOKAFORGE_TRACING_METADATA", "task_id=forged")
        with pytest.raises(TracingConfigError, match="task_id"):
            merge_metadata({}, profile)
        monkeypatch.delenv("TOLOKAFORGE_TRACING_METADATA")
        # the verdict keys are the projection's
        with pytest.raises(TracingConfigError, match="score"):
            merge_metadata({"score": 1}, profile)


class TestEnvironmentPrecedence:
    def test_variable_over_config_over_profile_rule(self, monkeypatch, tmp_path: Path) -> None:
        profile = load_tracing_profile(write_profile(tmp_path))
        monkeypatch.delenv("LANGFUSE_ENVIRONMENT", raising=False)
        assert resolve_environment(None, profile, ["run_kind:eval"]) == "production"
        assert resolve_environment("staging", profile, ["run_kind:eval"]) == "staging"
        monkeypatch.setenv("LANGFUSE_ENVIRONMENT", "development")
        assert resolve_environment("staging", profile, ["run_kind:eval"]) == "development"
        monkeypatch.setenv("LANGFUSE_ENVIRONMENT", "Not Valid")
        with pytest.raises(TracingConfigError, match="not a valid environment"):
            resolve_environment(None, profile, [])
        monkeypatch.delenv("LANGFUSE_ENVIRONMENT")
        assert resolve_environment(None, NO_PROFILE, ["run_kind:eval"]) is None


class TestTagSources:
    def test_origins_follow_the_first_source_and_clashes_refuse(self) -> None:
        merged, origins = merge_tag_sources(
            ("config", ["dataset:v1"]),
            ("launcher", ["config:stem", "dataset:v1"]),
            ("profile", ["team:pilot"]),
        )
        assert merged == ["dataset:v1", "config:stem", "team:pilot"]
        assert origins == {"dataset": "config", "config": "launcher", "team": "profile"}
        with pytest.raises(TracingConfigError, match="twice"):
            merge_tag_sources(("config", ["dataset:v1"]), ("profile", ["dataset:v3"]))


class TestProducerVersion:
    def test_version_joins_release_rules_and_profile(self, tmp_path: Path) -> None:
        profile = load_tracing_profile(write_profile(tmp_path))
        assert producer_version("tolokaforge-0.1.0", "rules-1", profile) == (
            "tolokaforge-0.1.0+rules-1+acme-2026.09.17.1"
        )
        assert producer_version("tolokaforge-0.1.0", "none", NO_PROFILE) == "tolokaforge-0.1.0"


class TestFactory:
    @pytest.fixture
    def clean_env(self, monkeypatch):
        for name in (
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "TOLOKAFORGE_TRACING_TAGS",
            "TOLOKAFORGE_TRACING_EXPECT_PROJECT",
            "TOLOKAFORGE_TRACING_PROFILE",
            "TOLOKAFORGE_TRACING_METADATA",
            "LANGFUSE_ENVIRONMENT",
            "LANGFUSE_TRACING_ENABLED",
            "LANGFUSE_BASE_URL",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "LANGFUSE_PROJECT",
        ):
            monkeypatch.delenv(name, raising=False)
        return monkeypatch

    def _build(self, tmp_path: Path, **tracing):
        pytest.importorskip("opentelemetry.sdk")
        params: dict = {
            "exporter": "otlp",
            "endpoint": "http://127.0.0.1:9/api/public/otel/v1/traces",
            "run_id": "acme/pilot/1",
            "attach": "none",
        }
        params.update(tracing)
        config = ObservabilityConfig(tracing=TracingConfig(**params))
        return build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)

    def test_the_profile_shapes_the_observer(self, clean_env, tmp_path: Path) -> None:
        path = write_profile(tmp_path)
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", "run_kind:eval,dataset:pilot")
        clean_env.setenv("TOLOKAFORGE_TRACING_METADATA", "run_label=nightly")
        observer, _ = self._build(tmp_path, profile=str(path), tags=["config:stem"])
        try:
            settings = observer._projection
            assert settings.environment == "production"
            assert settings.release.startswith("tolokaforge-")
            assert settings.version.endswith("+acme-2026.09.17.1")
            assert settings.producer == settings.release
            assert observer._tags == ("config:stem", "run_kind:eval", "dataset:pilot", "team:pilot")
            assert observer._metadata == {"deployment": "pilot", "run_label": "nightly"}
        finally:
            observer.run_finished()
        identity = json.loads((tmp_path / RUN_IDENTITY_FILE).read_text())
        assert identity["engine_version"] and identity["run_id"] == "acme/pilot/1"

    def test_the_profile_comes_from_the_variable_and_the_environment_override_wins(
        self, clean_env, tmp_path: Path
    ) -> None:
        clean_env.setenv("TOLOKAFORGE_TRACING_PROFILE", str(write_profile(tmp_path)))
        clean_env.setenv("LANGFUSE_ENVIRONMENT", "development")
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", "run_kind:eval")
        observer, _ = self._build(tmp_path)
        try:
            assert observer._projection.environment == "development"
        finally:
            observer.run_finished()

    def test_no_profile_leaves_the_environment_unset(self, clean_env, tmp_path: Path) -> None:
        observer, _ = self._build(tmp_path)
        try:
            assert observer._projection.environment is None
            assert observer._projection.version == observer._projection.release
        finally:
            observer.run_finished()

    def test_a_broken_profile_a_clash_and_a_bad_environment_are_run_start_errors(
        self, clean_env, tmp_path: Path
    ) -> None:
        with pytest.raises(TracingConfigError, match="'schema' must be 1"):
            self._build(tmp_path, profile=str(write_profile(tmp_path, "schema = 9\n", "bad.toml")))
        with pytest.raises(TracingConfigError, match="no such file|cannot be read"):
            self._build(tmp_path, profile=str(tmp_path / "missing.toml"))
        clean_env.setenv("TOLOKAFORGE_TRACING_METADATA", "status=forged")
        with pytest.raises(TracingConfigError, match="status"):
            self._build(tmp_path)
        clean_env.delenv("TOLOKAFORGE_TRACING_METADATA")
        with pytest.raises(TracingConfigError, match="not a valid environment"):
            self._build(tmp_path, environment="Prod")

    def test_attach_none_with_gradings_off_still_sends_the_full_projection(
        self, clean_env, tmp_path: Path
    ) -> None:
        observer, _ = self._build(tmp_path, gradings=False)
        try:
            assert observer._attachments is not None  # the trial-end pass needs its route
        finally:
            observer.run_finished()
        observer, _ = self._build(tmp_path, gradings=False, projection="gradings")
        try:
            assert observer._attachments is None  # nothing to send at trial end
        finally:
            observer.run_finished()
        observer, _ = self._build(tmp_path, projection="none")
        try:
            assert observer._attachments is None
        finally:
            observer.run_finished()

    def test_the_manifest_update_carries_the_environment(self, clean_env, tmp_path: Path) -> None:
        clean_env.setenv("LANGFUSE_ENVIRONMENT", "staging")
        observer, _ = self._build(tmp_path, attach="all")
        try:
            assert observer._attachments._environment == "staging"
        finally:
            observer.run_finished()

    def test_a_profile_fixed_tag_clashing_with_the_launcher_refuses(
        self, clean_env, tmp_path: Path
    ) -> None:
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", "team:other")
        with pytest.raises(TracingConfigError, match="team"):
            self._build(tmp_path, profile=str(write_profile(tmp_path)))

    def test_the_profiles_rules_select_the_normalizer(self, clean_env, tmp_path: Path) -> None:
        pytest.importorskip("toloka_model_name_normalizer")
        (tmp_path / "rules.toml").write_text(
            'schema_version = 1\nversion = "acme-rules"\n[lookup."acme/x1"]\nfamily = "xline"\nwhy = "test"\n'
        )
        path = write_profile(
            tmp_path, 'schema = 1\nversion = "p"\n[models]\nrules = "rules.toml"\n'
        )
        observer, _ = self._build(tmp_path, profile=str(path))
        try:
            assert observer._resolver.rules_version == "acme-rules"
            assert observer._projection.version.endswith("+acme-rules+p")
        finally:
            observer.run_finished()

    def test_the_projection_mode_is_locked_to_full_by_default(self) -> None:
        assert TracingConfig().projection == "full" and TracingConfig().profile is None
        assert TracingConfig().environment is None
        with pytest.raises(ValueError):
            TracingConfig(projection="everything")
