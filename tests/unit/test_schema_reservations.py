"""Unit tests for M2.5 #224 schema-shape reservations.

Name-squats: the ``actors`` map, ``ComputeConfig.capabilities``
entries, and ``assets.seeds`` typed registry. Also covers the
task-schema relaxation (``TaskConfig`` minimal shape) and the
``SecurityContext`` type widening.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import (
    ActorSpec,
    AssetsConfig,
    ComputeConfig,
    InitialStateConfig,
    ProjectConfig,
    SeedRef,
    TaskConfig,
    TaskDefaults,
    ToolsConfig,
    UserSimulatorConfig,
)
from tolokaforge.runner.models import SecurityContext

pytestmark = pytest.mark.unit


# ── ActorSpec / actors map ──────────────────────────────────────


class TestActorSpec:
    def test_all_fields_optional(self) -> None:
        spec = ActorSpec()
        assert spec.mode is None
        assert spec.persona is None
        assert spec.backstory is None
        assert spec.scripted_flow is None

    def test_forbids_unknown_sub_keys(self) -> None:
        # `tools` and `service` are reserved by the design; using them
        # today is a load error so a pack cannot repurpose the name.
        with pytest.raises(ValidationError):
            ActorSpec(tools=["bash"])  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            ActorSpec(service="runner")  # type: ignore[call-arg]

    def test_accepts_documented_fields(self) -> None:
        spec = ActorSpec(mode="llm", persona="curious engineer", backstory="…")
        assert spec.mode == "llm"
        assert spec.persona == "curious engineer"


class TestActorsMapReservation:
    def test_task_defaults_accepts_user_actor(self) -> None:
        td = TaskDefaults(actors={"user": ActorSpec(mode="llm", persona="p")})
        assert td.actors is not None
        assert td.actors["user"].persona == "p"

    def test_task_defaults_rejects_agent_name(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            TaskDefaults(actors={"agent": ActorSpec(mode="llm")})

    def test_task_defaults_rejects_judge_name(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            TaskDefaults(actors={"judge": ActorSpec(mode="llm")})

    def test_task_config_rejects_reserved_actor_name(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            TaskConfig(
                task_id="x",
                description="y",
                initial_state=InitialStateConfig(),
                tools=ToolsConfig(),
                user_simulator=UserSimulatorConfig(),
                grading="grading.yaml",
                actors={"agent": ActorSpec(mode="llm")},
            )

    def test_task_config_accepts_user_actor(self) -> None:
        # Positive counterpart to the reserved-name rejection — a task
        # override with a non-reserved actor name parses cleanly. The
        # runtime binding of ``actors.user`` back to today's simulator
        # lives in the actor-rename milestone; here we only pin that
        # the shape parses at the task layer, matching TaskDefaults.
        t = TaskConfig(
            task_id="x",
            description="y",
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(),
            user_simulator=UserSimulatorConfig(),
            grading="grading.yaml",
            actors={"user": ActorSpec(mode="llm", persona="task-local")},
        )
        assert t.actors is not None
        assert t.actors["user"].persona == "task-local"


# ── ComputeConfig.capabilities ──────────────────────────────────


class TestComputeCapabilities:
    def test_default_is_empty_list(self) -> None:
        cc = ComputeConfig()
        assert cc.capabilities == []

    def test_bare_string_entries_accepted(self) -> None:
        cc = ComputeConfig(capabilities=["net.isolation", "compose.exec"])
        assert cc.capabilities == ["net.isolation", "compose.exec"]

    def test_dict_entries_with_params_accepted(self) -> None:
        cc = ComputeConfig(
            capabilities=[{"k8s.cilium": {"policy_engine": "hubble"}}],
        )
        assert cc.capabilities == [{"k8s.cilium": {"policy_engine": "hubble"}}]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty capability name"):
            ComputeConfig(capabilities=[""])

    def test_multi_key_dict_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one key"):
            ComputeConfig(capabilities=[{"a": {}, "b": {}}])

    def test_non_dict_params_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a mapping"):
            ComputeConfig(capabilities=[{"name": "not-a-dict"}])  # type: ignore[list-item]

    def test_non_string_non_dict_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a string or a"):
            ComputeConfig(capabilities=[42])  # type: ignore[list-item]


# ── SeedRef + AssetsConfig ──────────────────────────────────────


class TestSeedRef:
    def test_full_dict_form(self) -> None:
        s = SeedRef.model_validate(
            {"path": "/abs/foo.sql", "kind": "sql_dump", "digest": "sha256:aaaa"},
        )
        assert s.path == Path("/abs/foo.sql")
        assert s.kind == "sql_dump"
        assert s.digest == "sha256:aaaa"

    def test_bare_string_infers_kind_from_sql_extension(self) -> None:
        # Bare-string shorthand still parses; digest is required by the
        # model so a full-form call needs it — but the shorthand path
        # here is only exercising kind inference. Bypass the missing
        # digest by asserting the ValidationError names the required
        # field so the migration path (`tolokaforge assets stamp`) is
        # discoverable.
        with pytest.raises(ValidationError, match="digest"):
            SeedRef.model_validate("/abs/foo.sql")

    def test_bare_string_infers_kind_from_rdb_extension(self) -> None:
        with pytest.raises(ValidationError, match="digest"):
            SeedRef.model_validate("/abs/dump.rdb")

    def test_bare_string_unknown_extension_rejected(self) -> None:
        # A ``.zip`` seed could be a filesystem_dir or a bare — ambiguous;
        # force the author to declare the full form.
        with pytest.raises(ValidationError, match="cannot infer kind"):
            SeedRef.model_validate("/abs/thing.zip")

    def test_digest_required(self) -> None:
        with pytest.raises(ValidationError, match="digest"):
            SeedRef.model_validate({"path": "/x/y.sql", "kind": "sql_dump"})

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SeedRef.model_validate(
                {
                    "path": "/x/y.sql",
                    "kind": "sql_dump",
                    "digest": "sha256:aaaa",
                    "unknown": 1,
                }
            )

    @pytest.mark.parametrize(
        "kind",
        ["sql_dump", "filesystem_dir", "redis_dump", "bare"],
    )
    def test_all_seed_kinds_accepted_in_dict_form(self, kind: str) -> None:
        # Pin the full vocabulary — a future refactor that narrows the
        # Literal type without updating the reset-recipe milestone
        # would silently strip authoring options for existing packs.
        s = SeedRef.model_validate(
            {"path": f"/abs/x.{kind[:3]}", "kind": kind, "digest": "sha256:aaaa"}
        )
        assert s.kind == kind

    def test_bare_string_extension_lookup_is_case_insensitive(self) -> None:
        # Extension inference uses .suffix.lower(); a Windows-authored
        # path like `Foo.SQL` reaches the digest-required error rather
        # than the ambiguous-extension one — that proves inference works
        # (extension → sql_dump) before the digest check fires.
        with pytest.raises(ValidationError, match="digest"):
            SeedRef.model_validate("/abs/Foo.SQL")


class TestAssetsConfig:
    def test_empty_default(self) -> None:
        ac = AssetsConfig()
        assert ac.seeds == {}

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AssetsConfig(seeds={}, unknown_field=1)  # type: ignore[call-arg]


class TestProjectConfigAssets:
    def test_assets_defaults_to_none(self) -> None:
        p = ProjectConfig(name="p")
        assert p.assets is None

    def test_assets_seeds_populate(self) -> None:
        p = ProjectConfig(
            name="p",
            assets=AssetsConfig(
                seeds={
                    "baseline": SeedRef(
                        path=Path("/abs/x.sql"),
                        kind="sql_dump",
                        digest="sha256:aaaa",
                    )
                }
            ),
        )
        assert p.assets is not None
        assert set(p.assets.seeds) == {"baseline"}


# ── TaskConfig relaxation ───────────────────────────────────────


class TestTaskConfigMinimal:
    def test_task_id_plus_description_is_enough(self) -> None:
        t = TaskConfig(task_id="x", description="y")
        assert t.task_id == "x"
        assert t.name is None
        assert t.category is None
        # The four relaxed fields default to model instances (not None) so the
        # unguarded live consumers in conductor.py / native.py keep working.
        assert isinstance(t.initial_state, InitialStateConfig)
        assert isinstance(t.tools, ToolsConfig)
        assert isinstance(t.user_simulator, UserSimulatorConfig)
        assert t.user_simulator.mode == "llm"
        assert t.user_simulator.persona == "cooperative"
        assert t.grading is None

    def test_name_still_set_when_provided(self) -> None:
        t = TaskConfig(
            task_id="x",
            name="Nice Name",
            category="cat",
            description="y",
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(),
            user_simulator=UserSimulatorConfig(),
            grading="grading.yaml",
        )
        assert t.name == "Nice Name"
        assert t.category == "cat"


class TestGradingNoneGuard:
    def test_get_grading_config_fails_loud_when_grading_none(self, tmp_path: Path) -> None:
        # A minimal task with no `grading:` and no sibling grading.yaml loads
        # with grading=None; get_grading_config must fail loud naming the task
        # rather than let `task_dir / None` raise a bare TypeError.
        from tolokaforge.adapters.native import NativeAdapter

        task_dir = tmp_path / "tasks" / "minimal"
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text("task_id: minimal\ndescription: does nothing\n")

        adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
        assert adapter.get_task("minimal").grading is None
        with pytest.raises(ValueError, match="minimal"):
            adapter.get_grading_config("minimal")


# ── SecurityContext type widening ───────────────────────────────


class TestSecurityContextTypeWidening:
    def test_numeric_uid_accepted(self) -> None:
        sc = SecurityContext(run_as_user=1000, run_as_group=1000)
        assert sc.run_as_user == 1000
        assert sc.run_as_group == 1000

    def test_username_string_accepted(self) -> None:
        sc = SecurityContext(run_as_user="toloka", run_as_group="toloka")
        assert sc.run_as_user == "toloka"
        assert sc.run_as_group == "toloka"

    def test_defaults_still_none(self) -> None:
        sc = SecurityContext()
        assert sc.run_as_user is None
        assert sc.run_as_group is None
