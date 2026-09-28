"""The deployment's Langfuse block in the run configuration (ADR-0047, configuration amendment):
the profile loaders over TOML, YAML and inline input, the engine-free block reader, the project
and environments check, the preflight command in both modes, and the plugin anchoring its
relative paths to the project from any working directory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tolokaforge_langfuse.config import EnvironmentEntry, LangfuseConfig
from tolokaforge_langfuse.preflight import (
    PreflightError,
    anchor_directory,
    find_project_yaml,
    load_langfuse_block,
    main,
    resolve_plan,
    resolve_profile,
)
from tolokaforge_langfuse.profile import (
    NO_PROFILE,
    TracingProfileError,
    check_caller_inputs,
    load_tracing_profile,
    profile_from_mapping,
)

from tolokaforge.core.models import ObservabilityConfig, TracingConfig
from tolokaforge.observability.factory import TracingConfigError, build_trial_observer
from tolokaforge_langfuse import __version__

pytestmark = pytest.mark.unit

PROFILE = {
    "schema": 2,
    "version": "pilot-2026.10.01.1",
    "tags": {
        "fixed": ["team:pilot"],
        "values": {"dataset": ["v1", "v3"], "domain": ["billing", "support"]},
        "required": {"trial": ["dataset", "scope", "domain", "config"]},
    },
    "derive": {"scope": {"full": "full", "sample": "sample"}},
    "metadata": {"keys": ["model_stem"]},
}
BLOCK = {
    "profile": PROFILE,
    "project": "pilot",
    "project_id": "pilot-project-id",
    "environments": {
        "test": {"accepts": ["trial"]},
        "test-automation": {"accepts": ["transcript"]},
        "production": {"accepts": ["trial"]},
    },
}
LAUNCHER_TAGS = "config:agent,run_kind:eval,scope:full,dataset:v1"


def project_file(root: Path, block: dict | None = None, *, name: str = "project.yaml") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "name": "pilot",
        "run_defaults": {"observability": {"tracing": {"options": {"langfuse": block or BLOCK}}}},
    }
    path = root / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def run_config(root: Path, domain: str = "billing") -> Path:
    path = root / "config" / domain / "agent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"observability": {"tracing": {"tags": [f"domain:{domain}"]}}}),
        encoding="utf-8",
    )
    return path


class _Tracing:
    def __init__(self, options: dict, tags=(), metadata=None) -> None:
        self.options, self.tags, self.metadata = options, list(tags), dict(metadata or {})


class TestProfileLoaders:
    def test_toml_yaml_and_inline_give_the_same_profile(self, tmp_path: Path) -> None:
        (tmp_path / "rules.toml").write_text('version = "r"\n', encoding="utf-8")
        body = {**PROFILE, "models": {"rules": "rules.toml"}}
        yaml_path = tmp_path / "profile.yaml"
        yaml_path.write_text(yaml.safe_dump(body), encoding="utf-8")
        toml_path = tmp_path / "profile.toml"
        toml_path.write_text(
            'schema = 2\nversion = "pilot-2026.10.01.1"\n[tags]\nfixed = ["team:pilot"]\n'
            '[tags.values]\ndataset = ["v1", "v3"]\ndomain = ["billing", "support"]\n'
            '[tags.required]\ntrial = ["dataset", "scope", "domain", "config"]\n'
            '[derive.scope]\nfull = "full"\nsample = "sample"\n'
            '[metadata]\nkeys = ["model_stem"]\n[models]\nrules = "rules.toml"\n',
            encoding="utf-8",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        from_yaml = load_tracing_profile(yaml_path)
        from_toml = load_tracing_profile(toml_path)
        inline = profile_from_mapping(body, base_dir=tmp_path)
        for profile in (from_yaml, from_toml, inline):
            assert profile.present
            assert profile.fixed_tags == ("team:pilot",)
            assert profile.model_name_rules == str(tmp_path / "rules.toml")
        # an inline profile's rules anchor to base_dir, never to the working directory
        with pytest.raises(TracingProfileError, match="no such file"):
            profile_from_mapping(body, base_dir=elsewhere)
        assert profile_from_mapping(body, check_files=False).model_name_rules == "rules.toml"

    def test_an_inline_profile_enforces_its_required_tags(self) -> None:
        profile = profile_from_mapping(PROFILE)
        assert profile.present and not NO_PROFILE.present
        with pytest.raises(TracingProfileError, match="missing required tag.*domain"):
            check_caller_inputs(
                profile, ["team:pilot", "run_kind:eval", "dataset:v1", "scope:full"]
            )

    def test_yaml_that_does_not_parse_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.yml"
        path.write_text("schema: [2\n", encoding="utf-8")
        with pytest.raises(TracingProfileError, match="not valid YAML"):
            load_tracing_profile(path)


class TestConfigModel:
    def test_the_block_validates(self) -> None:
        config = LangfuseConfig.model_validate(BLOCK)
        assert config.expected_project() == "pilot"
        assert config.environments["test"] == EnvironmentEntry(accepts=("trial",))
        assert not config.environments["test-automation"].admits("trial")
        assert EnvironmentEntry(accepts=("any",)).admits("transcript")

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"environments": {}}, "at least one environment"),
            ({"environments": {"Test": {"accepts": ["trial"]}}}, "not a valid environment"),
            ({"environments": {"test": {"accepts": []}}}, "at least 1"),
            ({"environments": {"test": {"accepts": ["trials"]}}}, "trial"),
            ({"environments": {"test": {"accepts": ["trial"], "key": "x"}}}, "extra_forbidden"),
            ({"environment": "test"}, "exclude each other"),
            ({"expect_project": "other"}, "contradicts project"),
            ({"project": None}, "project_id needs project"),
            ({"profile": {"schema": 9}}, "'schema' must be one of"),
            ({"profile": " "}, "non-empty path"),
        ],
    )
    def test_invalid_blocks_are_refused(self, change: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            LangfuseConfig.model_validate({**BLOCK, **change})


class TestBlockReader:
    def test_reads_a_project_file_and_anchors_to_its_directory(self, tmp_path: Path) -> None:
        path = project_file(tmp_path / "repo")
        block = load_langfuse_block(path)
        assert block.base_dir == (tmp_path / "repo").resolve()
        assert block.config.project == "pilot"
        # any file name: the sandbox variant of a project file is one
        other = project_file(tmp_path / "repo", name="project.sandbox.yaml")
        assert load_langfuse_block(other).config == block.config

    def test_reads_a_run_config_at_the_top_level(self, tmp_path: Path) -> None:
        path = tmp_path / "run.yaml"
        path.write_text(
            yaml.safe_dump({"observability": {"tracing": {"options": {"langfuse": BLOCK}}}}),
            encoding="utf-8",
        )
        assert load_langfuse_block(path).config.environments is not None

    def test_a_misspelt_parent_key_is_named(self, tmp_path: Path) -> None:
        path = tmp_path / "project.yaml"
        path.write_text(
            yaml.safe_dump({"name": "pilot", "run_defaults": {"observabilty": {"tracing": {}}}}),
            encoding="utf-8",
        )
        with pytest.raises(PreflightError, match=r"no run_defaults.observability.*observabilty"):
            load_langfuse_block(path)

    def test_the_block_at_the_top_of_a_project_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "project.yaml"
        path.write_text(
            yaml.safe_dump({"name": "pilot", "observability": {"tracing": {}}}), encoding="utf-8"
        )
        with pytest.raises(PreflightError, match="under run_defaults"):
            load_langfuse_block(path)

    def test_a_placeholder_inside_the_block_is_refused(self, tmp_path: Path) -> None:
        block = {**BLOCK, "project": "${LANGFUSE_PROJECT}"}
        with pytest.raises(PreflightError, match=r"placeholders.*options.langfuse.project"):
            load_langfuse_block(project_file(tmp_path, block))

    def test_a_file_without_the_block_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "run.yaml"
        path.write_text(yaml.safe_dump({"observability": {"tracing": {"tags": []}}}), "utf-8")
        with pytest.raises(PreflightError, match="no observability.tracing.options.langfuse"):
            load_langfuse_block(path)

    def test_the_project_walk_matches_the_engine_loader(self, tmp_path: Path) -> None:
        from tolokaforge.core.project_loader import find_project_yaml as engine_find

        path = project_file(tmp_path)
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert find_project_yaml(deep) == engine_find(deep) == path.resolve()
        assert anchor_directory(deep) == tmp_path.resolve()
        too_deep = tmp_path.joinpath(*"123456789")
        too_deep.mkdir(parents=True)
        assert find_project_yaml(too_deep) == engine_find(too_deep) is None
        assert anchor_directory(too_deep) == too_deep.resolve()


class TestPlan:
    def plan(self, tmp_path: Path, environ: dict, block: dict | None = None, tags=()):
        return resolve_plan(_Tracing({"langfuse": block or BLOCK}, tags=tags), environ, tmp_path)

    def environ(self, **extra: str) -> dict:
        return {"LANGFUSE_ENVIRONMENT": "test", "TOLOKAFORGE_TRACING_TAGS": LAUNCHER_TAGS, **extra}

    def test_the_plan_of_a_trial(self, tmp_path: Path) -> None:
        plan = self.plan(tmp_path, self.environ(), tags=["domain:billing"])
        assert plan.environment == "test" and plan.expect_project == "pilot"
        assert plan.tags[-2:] == ("team:pilot", "project:pilot")
        assert plan.origins["domain"] == "config" and plan.origins["project"] == "receiver"
        assert plan.version == f"tolokaforge-langfuse-{__version__}+pilot-2026.10.01.1"

    @pytest.mark.parametrize(
        ("environ", "message"),
        [
            ({"LANGFUSE_ENVIRONMENT": ""}, "LANGFUSE_ENVIRONMENT is required"),
            ({"LANGFUSE_ENVIRONMENT": "staging"}, "'staging' is not a declared environment"),
            (
                {"LANGFUSE_ENVIRONMENT": "test-automation"},
                r"accepts \['transcript'\], not source:trial",
            ),
            ({"TOLOKAFORGE_TRACING_EXPECT_PROJECT": "other"}, "contradicts the block's project"),
            ({"LANGFUSE_PROJECT": "other"}, "contradicts the block's project"),
            ({"TOLOKAFORGE_TRACING_TAGS": "config:agent"}, "missing required tag"),
            ({"TOLOKAFORGE_TRACING_TAGS": LAUNCHER_TAGS + ",team:other"}, "team"),
        ],
    )
    def test_refusals_name_the_reason(self, tmp_path: Path, environ: dict, message: str) -> None:
        with pytest.raises(PreflightError, match=message):
            self.plan(tmp_path, self.environ(**environ), tags=["domain:billing"])

    def test_a_config_tag_contradicting_the_launcher_names_both_sources(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(
            PreflightError, match=r"'billing' \(config\) and 'support' \(launcher\)"
        ):
            self.plan(
                tmp_path,
                self.environ(TOLOKAFORGE_TRACING_TAGS=LAUNCHER_TAGS + ",domain:support"),
                tags=["domain:billing"],
            )

    def test_a_profile_variable_next_to_an_inline_profile_warns(self, tmp_path: Path) -> None:
        plan = self.plan(
            tmp_path,
            self.environ(TOLOKAFORGE_TRACING_PROFILE="/nowhere/profile.toml"),
            tags=["domain:billing"],
        )
        assert plan.profile.version == "pilot-2026.10.01.1"
        assert any("TOLOKAFORGE_TRACING_PROFILE" in w and "inline" in w for w in plan.warnings)

    def test_a_profile_path_anchors_to_the_base_dir(self, tmp_path: Path) -> None:
        (tmp_path / "deploy").mkdir()
        (tmp_path / "deploy" / "profile.yaml").write_text(yaml.safe_dump(PROFILE), "utf-8")
        settings = LangfuseConfig(profile="deploy/profile.yaml")
        assert resolve_profile(settings, {}, tmp_path).version == "pilot-2026.10.01.1"
        with pytest.raises(PreflightError, match="cannot be read"):
            resolve_profile(settings, {}, tmp_path / "deploy")

    def test_without_environments_the_old_precedence_holds(self, tmp_path: Path) -> None:
        plan = self.plan(
            tmp_path,
            {"TOLOKAFORGE_TRACING_TAGS": "run_kind:eval,team:pilot,dataset:v1,scope:full"},
            block={"environment": "staging"},
        )
        assert plan.environment == "staging" and plan.expect_project is None


class TestPreflightCommand:
    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch) -> Path:
        for name in (
            "LANGFUSE_ENVIRONMENT",
            "TOLOKAFORGE_TRACING_TAGS",
            "TOLOKAFORGE_TRACING_METADATA",
            "TOLOKAFORGE_TRACING_PROFILE",
            "TOLOKAFORGE_TRACING_EXPECT_PROJECT",
            "LANGFUSE_PROJECT",
        ):
            monkeypatch.delenv(name, raising=False)
        root = tmp_path / "repo"
        project_file(root)
        run_config(root)
        monkeypatch.chdir(root)
        return root

    def golden(self, root: Path, mode: str, subject: Path, config_tag: str) -> str:
        tags = (
            f"{config_tag}config:agent (launcher), run_kind:eval (launcher), scope:full (launcher),"
            " dataset:v1 (launcher), team:pilot (profile), project:pilot (receiver)"
        )
        profile = (
            "schema 2, version pilot-2026.10.01.1, environment from tag run_kind ({'eval':"
            " 'production'}, default development), fixed tags ['team:pilot'], derived"
            " ['model_facets', 'reasoning', 'route'], values for ['dataset', 'domain'], required"
            " {'trial': ('dataset', 'scope', 'domain', 'config')}, derivations for ['scope'],"
            " metadata keys ['model_stem'], fixed metadata {}, model rules none"
        )
        return "\n".join(
            [
                f"OK {subject} ({mode})",
                "  project: pilot (id pilot-project-id)",
                "  environment: test",
                f"  tags: {tags}",
                "  metadata keys: ['model_stem']",
                f"  profile: {profile}",
                "  model-name rules: none",
                f"  version: tolokaforge-langfuse-{__version__}+pilot-2026.10.01.1",
                f"  relative paths anchor to: {root.resolve()}",
                "",
            ]
        )

    def run(self, capsys, *args: str) -> tuple[int, str, str]:
        code = main(list(args))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_engine_layering_prints_the_plan(self, repo: Path, capsys) -> None:
        config = Path("config/billing/agent.yaml")
        code, out, err = self.run(
            capsys,
            "--config",
            str(config),
            "--environment",
            "test",
            "--tags",
            LAUNCHER_TAGS,
            "--metadata",
            "model_stem=agent",
        )
        assert code == 0, err
        assert out == self.golden(repo, "engine layering", config, "domain:billing (config), ")

    def test_offline_reads_the_project_file_alone(self, repo: Path, capsys) -> None:
        code, out, err = self.run(
            capsys,
            "--config",
            "project.yaml",
            "--offline",
            "--environment",
            "test",
            "--tags",
            "domain:billing," + LAUNCHER_TAGS,
            "--metadata",
            "model_stem=agent",
        )
        assert code == 0, err
        expected = self.golden(repo, "offline", Path("project.yaml"), "domain:billing (launcher), ")
        assert out == expected

    def test_offline_over_a_run_config_layers_the_project_file_under_it(
        self, repo: Path, capsys
    ) -> None:
        """The merge job's view: no engine, the config's own domain tag checked like the live
        path checks it."""
        config = Path("config/billing/agent.yaml")
        code, out, err = self.run(
            capsys,
            "--config",
            str(config),
            "--offline",
            "--environment",
            "test",
            "--tags",
            LAUNCHER_TAGS,
            "--metadata",
            "model_stem=agent",
        )
        assert code == 0, err
        assert out == self.golden(repo, "offline", config, "domain:billing (config), ")
        code, _, err = self.run(
            capsys,
            "--config",
            str(config),
            "--offline",
            "--environment",
            "test",
            "--tags",
            LAUNCHER_TAGS + ",domain:support",
        )
        assert code == 2 and "'billing' (config) and 'support' (launcher)" in err

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--environment", "staging"], "'staging' is not a declared environment"),
            (["--environment", "test-automation"], "not source:trial"),
            (["--environment", "test", "--tags", "config:agent"], "missing required tag"),
            (
                ["--environment", "test", "--tags", LAUNCHER_TAGS + ",domain:support"],
                r"'billing' \(config\) and 'support' \(launcher\)",
            ),
        ],
    )
    def test_refusals_exit_2(self, repo: Path, capsys, args: list[str], message: str) -> None:
        code, out, err = self.run(
            capsys, "--config", "config/billing/agent.yaml", "--tags", LAUNCHER_TAGS, *args
        )
        assert code == 2 and out == ""
        assert "PREFLIGHT FAILED" in err
        assert __import__("re").search(message, err), err

    def test_a_misspelt_key_in_the_project_file_drops_the_block_and_exits_2(
        self, repo: Path, capsys
    ) -> None:
        text = (repo / "project.yaml").read_text(encoding="utf-8")
        (repo / "project.yaml").write_text(text.replace("observability:", "observabilty:"), "utf-8")
        for extra in ([], ["--offline"]):
            target = "project.yaml" if extra else "config/billing/agent.yaml"
            code, _, err = self.run(capsys, "--config", target, "--environment", "test", *extra)
            assert code == 2 and "no " in err and "observability.tracing.options.langfuse" in err

    def test_a_block_without_project_or_environments_exits_2(self, repo: Path, capsys) -> None:
        for missing, message in (("project", "no project"), ("environments", "no environments")):
            block = {k: v for k, v in BLOCK.items() if k not in (missing, "project_id")}
            project_file(repo, block)
            code, _, err = self.run(capsys, "--config", "project.yaml", "--offline")
            assert code == 2 and message in err

    def test_an_engine_without_the_seam_exits_2(self, repo: Path, capsys, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "tolokaforge.observability.factory", None)
        code, _, err = self.run(
            capsys, "--config", "config/billing/agent.yaml", "--environment", "test"
        )
        assert code == 2 and "no trial-observer seam" in err

    def test_an_unclaimed_options_namespace_warns(self, repo: Path, capsys) -> None:
        config = repo / "config" / "billing" / "agent.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "observability": {
                        "tracing": {"tags": ["domain:billing"], "options": {"langfus": {}}}
                    }
                }
            ),
            "utf-8",
        )
        code, _, err = self.run(
            capsys, "--config", str(config), "--environment", "test", "--tags", LAUNCHER_TAGS
        )
        assert code == 0 and "WARN options.langfus" in err


class TestPluginAnchoring:
    """The plugin gets the merged block without the file it came from: its relative paths
    anchor to the nearest project.yaml above the working directory."""

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

    def _fake_receiver(self, monkeypatch) -> None:
        from tolokaforge_langfuse import media

        def opener(method, url, headers, body, timeout):
            if media.V2_OBSERVATIONS_PATH in url:
                return (404, b"")  # a v3 receiver
            return (
                200,
                json.dumps({"data": [{"id": "pilot-project-id", "name": "pilot"}]}).encode(),
            )

        monkeypatch.setattr(media, "urllib_opener", opener)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic dGVzdDpzZWNyZXQ=")

    def build(self, block: dict, tmp_path: Path):
        pytest.importorskip("opentelemetry.sdk")
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                tags=["domain:billing"],
                options={"langfuse": {**block, "attach": "none"}},
            )
        )
        return build_trial_observer(config, engine_run_id="run-1", output_dir=tmp_path)

    def test_a_profile_path_resolves_from_a_foreign_working_directory(
        self, clean_env, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        project_file(root)
        (root / "deploy").mkdir()
        (root / "deploy" / "profile.yaml").write_text(yaml.safe_dump(PROFILE), "utf-8")
        foreign = root / "tools" / "deep" / "cwd"
        foreign.mkdir(parents=True)
        clean_env.chdir(foreign)
        self._fake_receiver(clean_env)
        clean_env.setenv("LANGFUSE_ENVIRONMENT", "test")
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", LAUNCHER_TAGS)
        observer, _ = self.build({**BLOCK, "profile": "deploy/profile.yaml"}, tmp_path)
        receipt = observer.run_finished()
        details = receipt.details[0]
        assert (details["environment"], details["profile_version"]) == (
            "test",
            "pilot-2026.10.01.1",
        )
        assert (details["expect_project"], details["project_verified"]) == ("pilot", "verified")
        assert "project:pilot" in observer._tags and "team:pilot" in observer._tags
        # the same block from a directory with no project above it misses the profile loudly
        outside = tmp_path / "outside"
        outside.mkdir()
        clean_env.chdir(outside)
        with pytest.raises(TracingConfigError, match="cannot be read"):
            self.build({**BLOCK, "profile": "deploy/profile.yaml"}, tmp_path)

    def test_model_rules_resolve_from_a_foreign_working_directory(
        self, clean_env, tmp_path: Path
    ) -> None:
        pytest.importorskip("toloka_model_name_normalizer")
        root = tmp_path / "repo"
        project_file(root)
        (root / "rules.toml").write_text(
            'schema_version = 1\nversion = "pilot-rules"\n[lookup."acme/x1"]\nfamily = "xline"\nwhy = "t"\n',
            "utf-8",
        )
        foreign = root / "sub"
        foreign.mkdir()
        clean_env.chdir(foreign)
        self._fake_receiver(clean_env)
        clean_env.setenv("LANGFUSE_ENVIRONMENT", "production")
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", LAUNCHER_TAGS)
        profile = {**PROFILE, "models": {"rules": "rules.toml"}}
        observer, _ = self.build({**BLOCK, "profile": profile}, tmp_path)
        try:
            assert observer._resolver.rules_version == "pilot-rules"
        finally:
            observer.run_finished()

    @pytest.mark.parametrize(
        ("environment", "message"),
        [
            (None, "LANGFUSE_ENVIRONMENT is required"),
            ("staging", "not a declared environment"),
            ("test-automation", "not source:trial"),
        ],
    )
    def test_the_environment_check_refuses_at_run_start(
        self, clean_env, tmp_path: Path, environment: str | None, message: str
    ) -> None:
        project_file(tmp_path)
        clean_env.chdir(tmp_path)
        self._fake_receiver(clean_env)
        clean_env.setenv("TOLOKAFORGE_TRACING_TAGS", LAUNCHER_TAGS)
        if environment:
            clean_env.setenv("LANGFUSE_ENVIRONMENT", environment)
        with pytest.raises(TracingConfigError, match=message):
            self.build(BLOCK, tmp_path)

    def test_the_receipt_details_stay_receiver_facts(self, clean_env, tmp_path: Path) -> None:
        """Without a profile or environments the new detail keys are present and empty."""
        pytest.importorskip("opentelemetry.sdk")
        clean_env.chdir(tmp_path)
        self._fake_receiver(clean_env)
        config = ObservabilityConfig(
            tracing=TracingConfig(
                exporter="otlp",
                endpoint="http://127.0.0.1:9/api/public/otel/v1/traces",
                options={"langfuse": {"attach": "none"}},
            )
        )
        observer, _ = build_trial_observer(config, engine_run_id="run-1")
        details = observer.run_finished().details[0]
        assert details["profile_version"] is None and details["environment"] == "development"
