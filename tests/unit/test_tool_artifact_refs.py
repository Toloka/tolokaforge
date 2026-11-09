"""Tool artefacts delivered by reference instead of inline (ADR-0068, ADR-0072).

A run that sets ``compute.trial_data_delivery: mount`` ships no bytes of a
task's files in its description: the native adapter records each file's
``pack:`` address, SHA-256 and size, the engine bind-mounts the task directory read-only into the
trial-scope runner, and the runner copies every file into the trial's artefact
directory, refusing the registration when any file is missing, leaves the mount
or does not hash to what the task was described with. Every place that cannot
read the mount refuses a description with references instead of skipping them.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.canonical._factories import write_yaml_file
from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.adapter_params import assemble_adapter_params
from tolokaforge.core.compose_materialisation import mount_tool_artifact_refs_into_runner
from tolokaforge.core.grading.bundle_producer import serialize_bundle_from_substrate
from tolokaforge.core.grading.kinds import CompositeGraderKind, GraderKindRefusedError
from tolokaforge.core.grading.tool_artifacts import (
    TOOL_ARTIFACT_REFS_DIR_ENV_VAR,
    TOOL_ARTIFACT_REFS_MOUNT_PATH,
    ToolArtifactRefError,
    extract_tool_artifacts,
    file_sha256,
)
from tolokaforge.core.models import ComputeConfig, EnvironmentPatch, RunConfig
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.tool_libraries import ToolLibraryError, merge_tool_libraries
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GradeDispatch
from tolokaforge.runner.models import (
    ArtifactRef,
    RunnerGradingConfig,
    TaskDescription,
)

pytestmark = pytest.mark.unit


_MINIMAL_RUN: dict[str, Any] = {
    "models": {},
    "orchestrator": {},
    "evaluation": {"task_packs": [], "output_dir": "/tmp/tolokaforge-test-out"},
}

_RUNNER_COMPOSE = {
    "services": {
        "runner": {"image": "tolokaforge-runner:local", "ports": ["50051"]},
        "db-service": {"image": "tolokaforge-db-service:local", "ports": ["8000"]},
    }
}


def _ref(rel: str, sha256: str = "0" * 64, size: int = 1) -> ArtifactRef:
    return ArtifactRef(uri=f"pack:{rel}", sha256=sha256, size=size)


def _ref_for(root: Path, rel: str) -> ArtifactRef:
    sha256, size = file_sha256(root / rel)
    return _ref(rel, sha256, size)


def _world(tmp_path: Path) -> Path:
    """A reference root holding a module, a nested data file and a state file."""
    root = tmp_path / "world"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "world_tool.py").write_text("ANSWER = 42\n")
    (root / "data").mkdir()
    (root / "data" / "records.json").write_bytes(json.dumps({"rows": list(range(1000))}).encode())
    (root / "case_state.json").write_text('{"case": 1}')
    return root


def _description(**fields: Any) -> TaskDescription:
    if fields.get("tool_artifact_refs") is not None:
        fields.setdefault("trial_data_delivery", "mount")
    return TaskDescription(
        task_id="refs-task",
        name="refs-task",
        category="test",
        description="refs",
        adapter_type="native",
        system_prompt="",
        **fields,
    )


def _discard_extraction(extract_dir: Path) -> None:
    """What ``extract_tool_artifacts`` asks of its caller once the artefacts are done with."""
    for entry in (str(extract_dir), str(extract_dir / "tools")):
        while entry in sys.path:
            sys.path.remove(entry)
    shutil.rmtree(extract_dir)


# ---------------------------------------------------------------------------
# The wire model
# ---------------------------------------------------------------------------


class TestWireModel:
    def test_a_reference_names_a_normalised_relative_pack_path(self) -> None:
        for path in ("/etc/passwd", "../outside.py", "a//b.py", "./a.py", "a\\b.py", ""):
            with pytest.raises(ValidationError, match="relative POSIX path"):
                ArtifactRef(uri=f"pack:{path}", sha256="0" * 64, size=0)

    @pytest.mark.parametrize("uri", ["a.py", "file:///abs/a.py", "/abs/a.py", "s3://b/a.py"])
    def test_a_reference_is_a_pack_address(self, uri: str) -> None:
        with pytest.raises(ValidationError, match="no 'pack:' scheme"):
            ArtifactRef(uri=uri, sha256="0" * 64, size=0)

    def test_a_reference_reads_back_its_pack_path(self) -> None:
        assert _ref("tools/a.py").pack_path == "tools/a.py"

    def test_a_reference_carries_a_lowercase_hex_sha256(self) -> None:
        for digest in ("0" * 63, "A" * 64, "z" * 64):
            with pytest.raises(ValidationError, match="64 lowercase hex"):
                _ref("a.py", digest, 0)

    def test_the_set_is_non_empty_and_its_targets_normalised(self) -> None:
        with pytest.raises(ValidationError, match="declares no files"):
            _description(tool_artifact_refs={})
        with pytest.raises(ValidationError, match="relative POSIX path"):
            _description(tool_artifact_refs={"../a.py": _ref("a.py")})

    def test_no_absolute_path_travels_in_the_description(self) -> None:
        description = _description(tool_artifact_refs={"a.py": _ref("tools/a.py")})
        wire = description.model_dump(mode="json")["tool_artifact_refs"]
        assert wire == {"a.py": {"uri": "pack:tools/a.py", "sha256": "0" * 64, "size": 1}}

    def test_an_artefact_is_delivered_inline_or_by_reference_never_both(self) -> None:
        with pytest.raises(ValidationError, match="both inline in tool_artifacts and referenced"):
            _description(
                tool_artifacts={"a.py": base64.b64encode(b"x").decode()},
                tool_artifact_refs={"a.py": _ref("a.py")},
            )


# ---------------------------------------------------------------------------
# Resolution: copy, verify, refuse
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_references_are_copied_beside_inline_artefacts_and_importable(
        self, tmp_path: Path
    ) -> None:
        root = _world(tmp_path)
        refs = {
            "tools/world_tool.py": _ref_for(root, "tools/world_tool.py"),
            "data/records.json": _ref_for(root, "data/records.json"),
            "_shared/initial_state.json": _ref_for(root, "case_state.json"),
        }
        inline = {"checks.py": base64.b64encode(b"CHECKS = True\n").decode()}

        extract_dir = extract_tool_artifacts("refs:0", inline, refs=refs, refs_dir=root)
        try:
            assert (extract_dir / "checks.py").read_bytes() == b"CHECKS = True\n"
            assert (extract_dir / "data" / "records.json").read_bytes() == (
                root / "data" / "records.json"
            ).read_bytes()
            assert (extract_dir / "_shared" / "initial_state.json").read_text() == '{"case": 1}'
            assert str(extract_dir / "tools") in sys.path
            assert (extract_dir / "tools" / "world_tool.py").is_file()
        finally:
            _discard_extraction(extract_dir)

    def test_a_changed_file_refuses_the_set_and_leaves_nothing_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _world(tmp_path)
        refs = {
            "tools/world_tool.py": _ref_for(root, "tools/world_tool.py"),
            "data/records.json": _ref_for(root, "data/records.json"),
        }
        (root / "tools" / "world_tool.py").write_text("ANSWER = 43\n")
        created: list[Path] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def _recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        monkeypatch.setattr("tempfile.mkdtemp", _recording_mkdtemp)
        path_before = list(sys.path)

        with pytest.raises(ToolArtifactRefError) as excinfo:
            extract_tool_artifacts("refs:1", {}, refs=refs, refs_dir=root)

        message = str(excinfo.value)
        assert "1 of 2" in message
        assert "tools/world_tool.py" in message
        assert refs["tools/world_tool.py"].sha256 in message
        assert sys.path == path_before
        assert created and not created[0].exists()

    def test_a_missing_file_and_a_size_mismatch_are_both_named(self, tmp_path: Path) -> None:
        root = _world(tmp_path)
        good = _ref_for(root, "case_state.json")
        refs = {
            "gone.py": _ref("gone.py"),
            "state.json": _ref(good.pack_path, good.sha256, good.size + 1),
        }

        with pytest.raises(ToolArtifactRefError) as excinfo:
            extract_tool_artifacts("refs:2", {}, refs=refs, refs_dir=root)

        message = str(excinfo.value)
        assert "2 of 2" in message
        assert "pack:gone.py is not a file" in message
        assert f"expected sha256 {good.sha256} and {good.size + 1} bytes" in message

    def test_a_long_list_of_refusals_is_counted_past_the_ones_it_shows(
        self, tmp_path: Path
    ) -> None:
        refs = {f"gone_{index}.py": _ref(f"gone_{index}.py") for index in range(7)}

        with pytest.raises(ToolArtifactRefError) as excinfo:
            extract_tool_artifacts("refs:many", {}, refs=refs, refs_dir=_world(tmp_path))

        message = str(excinfo.value)
        assert "7 of 7" in message
        assert "gone_4.py" in message
        assert "gone_5.py" not in message
        assert "; and 2 more." in message

    def test_a_symlink_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        root = _world(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("not the task's")
        (root / "escape.txt").symlink_to(secret)
        digest, size = file_sha256(secret)
        refs = {"escape.txt": _ref("escape.txt", digest, size)}

        with pytest.raises(ToolArtifactRefError, match="resolves outside the reference root"):
            extract_tool_artifacts("refs:3", {}, refs=refs, refs_dir=root)

    def test_references_without_a_root_are_refused_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        refs = {"a.py": _ref("a.py")}

        with pytest.raises(ToolArtifactRefError, match=TOOL_ARTIFACT_REFS_DIR_ENV_VAR):
            extract_tool_artifacts("refs:4", {}, refs=refs, refs_dir=None)

    def test_a_root_that_is_not_a_directory_is_refused(self, tmp_path: Path) -> None:
        refs = {"a.py": _ref("a.py")}

        with pytest.raises(ToolArtifactRefError, match="is not a directory"):
            extract_tool_artifacts("refs:5", {}, refs=refs, refs_dir=tmp_path / "absent")

    def test_file_sha256_covers_a_file_longer_than_its_chunk(self, tmp_path: Path) -> None:
        payload = b"x" * (3 * 1024 * 1024 + 17)
        (tmp_path / "big.bin").write_bytes(payload)

        assert file_sha256(tmp_path / "big.bin") == (
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )


# ---------------------------------------------------------------------------
# The runner at RegisterTrial
# ---------------------------------------------------------------------------


def _description_with_refs(root: Path) -> dict[str, Any]:
    refs = {"tools/world_tool.py": _ref_for(root, "tools/world_tool.py")}
    return {
        "task_id": "refs_task",
        "name": "refs task",
        "category": "test",
        "description": "a task delivering artefacts by reference",
        "adapter_type": "native",
        "system_prompt": "",
        "tool_artifact_refs": {target: ref.model_dump(mode="json") for target, ref in refs.items()},
        "trial_data_delivery": "mount",
    }


class TestRegisterTrial:
    def test_the_runner_resolves_references_from_its_mount(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "refs_task:0"

        response = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(_description_with_refs(root), trial_id=trial_id),
                trial_id=trial_id,
            ),
            mock_grpc_context,
        )
        try:
            assert response.success is True, response.error
            extracted = runner_service._artifact_dirs[trial_id]
            assert (extracted / "tools" / "world_tool.py").read_text() == "ANSWER = 42\n"
        finally:
            runner_service._cleanup_trial_artifacts(trial_id)

    def test_a_runner_without_a_mount_refuses_the_trial(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.delenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, raising=False)
        trial_id = "refs_task:1"

        response = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(_description_with_refs(root), trial_id=trial_id),
                trial_id=trial_id,
            ),
            mock_grpc_context,
        )

        assert response.success is False
        assert TOOL_ARTIFACT_REFS_DIR_ENV_VAR in response.error
        assert trial_id not in runner_service.trials
        assert trial_id not in runner_service._artifact_dirs

    def test_a_mismatched_mount_refuses_the_trial(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        description = _description_with_refs(root)
        (root / "tools" / "world_tool.py").write_text("ANSWER = 0\n")
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "refs_task:2"

        response = runner_service.RegisterTrial(
            register_request(trial_spec_json(description, trial_id=trial_id), trial_id=trial_id),
            mock_grpc_context,
        )

        assert response.success is False
        assert "do not resolve" in response.error
        assert "tools/world_tool.py" in response.error
        assert trial_id not in runner_service._artifact_dirs


# ---------------------------------------------------------------------------
# The delivery is the run's, not the task's
# ---------------------------------------------------------------------------


def _compose(tmp_path: Path) -> Path:
    compose = tmp_path / "environment.compose.yaml"
    write_yaml_file(compose, _RUNNER_COMPOSE)
    return compose


class TestDeliveryIsARunSetting:
    def test_a_task_manifest_no_longer_names_a_delivery(self) -> None:
        with pytest.raises(ValidationError, match="compute.trial_data_delivery"):
            EnvironmentPatch.model_validate({"tool_artifacts_delivery": "mount"})

    def test_inline_is_the_run_default(self) -> None:
        assert RunConfig.model_validate(_MINIMAL_RUN).compute is None
        assert ComputeConfig().trial_data_delivery == "inline"

    def test_the_run_setting_reaches_the_adapter(self) -> None:
        config = RunConfig.model_validate(
            {**_MINIMAL_RUN, "compute": {"trial_data_delivery": "mount"}}
        )
        assert assemble_adapter_params(config)["trial_data_delivery"] == "mount"
        assert (
            assemble_adapter_params(RunConfig.model_validate(_MINIMAL_RUN))["trial_data_delivery"]
            == "inline"
        )


# ---------------------------------------------------------------------------
# The native adapter
# ---------------------------------------------------------------------------


def _mcp_pack(
    tmp_path: Path,
    *,
    delivery: str | None,
    with_corpus: bool = False,
    shared_runner: bool = False,
    adapter: type[NativeAdapter] = NativeAdapter,
) -> NativeAdapter:
    """A shared-domain-shaped MCP pack: the server under ``_shared/``, the state per case."""
    task_dir = tmp_path / "tasks" / "refs_pack"
    (task_dir / "_shared").mkdir(parents=True)
    (task_dir / "_shared" / "mcp_server.py").write_text("# stub server\n")
    (task_dir / "_shared" / "world").mkdir()
    for index in range(3):
        (task_dir / "_shared" / "world" / f"record_{index}.json").write_text(
            json.dumps({"id": index})
        )
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "case_state.json").write_text('{"orders": []}')
    (task_dir / "fixtures").mkdir()
    (task_dir / "fixtures" / "tools.json").write_text(json.dumps([]))
    write_yaml_file(task_dir / "environment.compose.yaml", _RUNNER_COMPOSE)
    initial_state: dict[str, Any] = {"json_db": "case_state.json"}
    if with_corpus:
        (task_dir / "kb").mkdir()
        (task_dir / "kb" / "policy.md").write_text("# Policy\n")
        initial_state["rag"] = {"corpus_dir": "kb"}
    environment: dict[str, Any] = {
        "stack": {"compose_file": "environment.compose.yaml", "runner_service": "runner"}
    }
    if shared_runner:
        environment["services"] = {
            name: {"isolation": "shared"} for name in _RUNNER_COMPOSE["services"]
        }
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "refs_pack",
            "name": "refs_pack",
            "category": "tool_use",
            "description": "a pack delivering its world by reference",
            "initial_state": initial_state,
            "tools": {
                "agent": {
                    "mcp_server": "_shared/mcp_server.py",
                    "enabled": ["search_kb"] if with_corpus else [],
                },
                "user": {"enabled": []},
            },
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
            "environment_manifest": environment,
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )
    params: dict[str, Any] = {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
    if delivery is not None:
        params["trial_data_delivery"] = delivery
    return adapter(params)


class _OwnSetAdapter(NativeAdapter):
    """An adapter that ships the task directory's binary blobs too, through the sources hook."""

    def _task_artifact_sources(self, task_dir: Path) -> dict[str, Path]:
        return {
            **super()._task_artifact_sources(task_dir),
            **{p.relative_to(task_dir).as_posix(): p for p in sorted(task_dir.rglob("*.bin"))},
        }


class _UnreadableFileAdapter(NativeAdapter):
    """An adapter whose set names a file that cannot be read when the task is described."""

    def _task_artifact_sources(self, task_dir: Path) -> dict[str, Path]:
        return {**super()._task_artifact_sources(task_dir), "gone.py": task_dir / "gone.py"}


class _OwnCorpusBundleAdapter(NativeAdapter):
    """An adapter that overrides the corpus bundler alone."""

    def _bundle_corpus_artifacts(self, task_dir: Path, corpus_dir: str) -> dict[str, str]:
        return {f"{corpus_dir}/own.md": base64.b64encode(b"own").decode("ascii")}


class _OwnBundleAdapter(NativeAdapter):
    """An adapter that overrides the inline bundler alone, as one written before ADR-0068 would."""

    def _bundle_task_artifacts(self, task_dir: Path) -> dict[str, str]:
        return {
            p.relative_to(task_dir).as_posix(): base64.b64encode(p.read_bytes()).decode("ascii")
            for p in sorted(task_dir.rglob("*"))
            if p.is_file()
        }


class TestNativeAdapter:
    def test_an_adapter_s_own_set_reaches_both_deliveries_through_the_sources_hook(
        self, tmp_path: Path
    ) -> None:
        adapters = {
            name: _mcp_pack(tmp_path / name, delivery=delivery, adapter=_OwnSetAdapter)
            for name, delivery in (("inline", None), ("mount", "mount"))
        }
        for name in adapters:
            (
                tmp_path / name / "tasks" / "refs_pack" / "_shared" / "world" / "blob.bin"
            ).write_bytes(b"\x00\x01\x02")
        inline = adapters["inline"].to_task_description("refs_pack")
        mounted = adapters["mount"].to_task_description("refs_pack")

        assert base64.b64decode(inline.tool_artifacts["_shared/world/blob.bin"]) == b"\x00\x01\x02"
        assert mounted.tool_artifact_refs is not None
        assert set(mounted.tool_artifact_refs) == set(inline.tool_artifacts)
        assert mounted.tool_artifact_refs["_shared/world/blob.bin"].size == 3

    def test_an_adapter_that_overrides_the_bundler_alone_is_refused_under_mount(
        self, tmp_path: Path
    ) -> None:
        inline = _mcp_pack(
            tmp_path / "inline", delivery=None, adapter=_OwnBundleAdapter
        ).to_task_description("refs_pack")
        assert "grading.yaml" in inline.tool_artifacts  # its own set, inline as before

        with pytest.raises(ValueError, match="overrides _bundle_task_artifacts alone"):
            _mcp_pack(
                tmp_path / "mount", delivery="mount", adapter=_OwnBundleAdapter
            ).to_task_description("refs_pack")

    def test_an_adapter_that_overrides_the_corpus_bundler_alone_is_refused_under_mount(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="overrides _bundle_corpus_artifacts alone"):
            _mcp_pack(
                tmp_path, delivery="mount", with_corpus=True, adapter=_OwnCorpusBundleAdapter
            ).to_task_description("refs_pack")

    @pytest.mark.parametrize("delivery", [None, "mount"])
    def test_a_file_that_cannot_be_read_fails_the_description_under_either_delivery(
        self, tmp_path: Path, delivery: str | None
    ) -> None:
        adapter = _mcp_pack(tmp_path, delivery=delivery, adapter=_UnreadableFileAdapter)

        with pytest.raises(FileNotFoundError, match="gone.py"):
            adapter.to_task_description("refs_pack")

    def test_mount_delivers_every_file_by_reference_and_none_inline(self, tmp_path: Path) -> None:
        inline = _mcp_pack(tmp_path / "inline", delivery=None).to_task_description("refs_pack")
        mounted = _mcp_pack(tmp_path / "mount", delivery="mount").to_task_description("refs_pack")

        assert inline.tool_artifact_refs is None
        assert mounted.tool_artifacts == {}
        refs = mounted.tool_artifact_refs
        assert refs is not None
        assert set(refs) == set(inline.tool_artifacts)
        task_dir = (tmp_path / "mount" / "tasks" / "refs_pack").resolve()
        for target, ref in refs.items():
            assert ref.uri.startswith("pack:"), target
            content = (task_dir / ref.pack_path).read_bytes()
            assert content == base64.b64decode(inline.tool_artifacts[target]), target
            assert ref.sha256 == hashlib.sha256(content).hexdigest(), target
            assert ref.size == len(content)

    def test_no_path_of_the_describing_machine_is_in_the_description(self, tmp_path: Path) -> None:
        mounted = _mcp_pack(tmp_path, delivery="mount").to_task_description("refs_pack")

        assert str(tmp_path) not in mounted.model_dump_json(include={"tool_artifact_refs"})

    def test_a_task_whose_runner_the_run_owns_stays_inline(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        adapter = _mcp_pack(tmp_path, delivery="mount", shared_runner=True)

        description = adapter.to_task_description("refs_pack")

        assert description.tool_artifact_refs is None
        assert "_shared/mcp_server.py" in description.tool_artifacts
        assert "no trial-scope stack owns the runner" in caplog.text

    def test_the_case_state_is_referenced_beside_the_shared_server(self, tmp_path: Path) -> None:
        description = _mcp_pack(tmp_path, delivery="mount").to_task_description("refs_pack")

        assert description.tool_artifact_refs is not None
        state = description.tool_artifact_refs["_shared/initial_state.json"]
        assert state.uri == "pack:case_state.json"

    def test_the_corpus_is_referenced_too(self, tmp_path: Path) -> None:
        description = _mcp_pack(tmp_path, delivery="mount", with_corpus=True).to_task_description(
            "refs_pack"
        )

        assert description.tool_artifact_refs is not None
        assert "kb/policy.md" in description.tool_artifact_refs

    def test_a_referenced_pack_extracts_to_the_same_tree_as_an_inline_one(
        self, tmp_path: Path
    ) -> None:
        inline = _mcp_pack(tmp_path / "inline", delivery=None).to_task_description("refs_pack")
        mounted = _mcp_pack(tmp_path / "mount", delivery="mount").to_task_description("refs_pack")
        assert mounted.tool_artifact_refs is not None

        inline_dir = extract_tool_artifacts("tree:inline", inline.tool_artifacts)
        mounted_dir = extract_tool_artifacts(
            "tree:mount",
            {},
            refs=mounted.tool_artifact_refs,
            refs_dir=tmp_path / "mount" / "tasks" / "refs_pack",
        )
        try:
            tree = {
                p.relative_to(inline_dir).as_posix(): p.read_bytes()
                for p in inline_dir.rglob("*")
                if p.is_file()
            }
            mounted_tree = {
                p.relative_to(mounted_dir).as_posix(): p.read_bytes()
                for p in mounted_dir.rglob("*")
                if p.is_file()
            }
            assert mounted_tree == tree
        finally:
            _discard_extraction(inline_dir)
            _discard_extraction(mounted_dir)


# ---------------------------------------------------------------------------
# Tool libraries keep their inline channel and collide with references
# ---------------------------------------------------------------------------


class _Library:
    name = "shared_lib"
    version = "1.0.0"

    def bundle(self, apps: Any) -> dict[str, bytes]:
        del apps
        return {"shared_lib/__init__.py": b"X = 1\n"}


def test_a_library_colliding_with_a_referenced_artefact_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tolokaforge.core import tool_libraries
    from tolokaforge.core.models.task_config import ToolLibraryPin

    monkeypatch.setattr(tool_libraries, "load_tool_library", lambda name: _Library())
    refs = {"shared_lib/__init__.py": _ref("shared_lib/__init__.py")}

    with pytest.raises(ToolLibraryError, match="shared_lib/__init__.py"):
        merge_tool_libraries(
            _description(tool_artifact_refs=refs),
            [ToolLibraryPin(name="shared_lib", version="1.0.0")],
        )


# ---------------------------------------------------------------------------
# The mount injected into a trial's compose file
# ---------------------------------------------------------------------------


class TestComposeMount:
    def test_the_root_is_mounted_read_only_and_named_to_the_runner(self, tmp_path: Path) -> None:
        compose = _compose(tmp_path)
        root = _world(tmp_path)

        mount_tool_artifact_refs_into_runner(compose, "runner", root)

        runner = yaml.safe_load(compose.read_text())["services"]["runner"]
        assert {
            "type": "bind",
            "source": str(root),
            "target": TOOL_ARTIFACT_REFS_MOUNT_PATH,
            "read_only": True,
        } in runner["volumes"]
        assert runner["environment"] == {
            TOOL_ARTIFACT_REFS_DIR_ENV_VAR: TOOL_ARTIFACT_REFS_MOUNT_PATH
        }
        assert "volumes" not in yaml.safe_load(compose.read_text())["services"]["db-service"]

    def test_a_list_environment_gains_the_variable(self, tmp_path: Path) -> None:
        compose = tmp_path / "environment.compose.yaml"
        doc = json.loads(json.dumps(_RUNNER_COMPOSE))
        doc["services"]["runner"]["environment"] = ["DB_SERVICE_URL=http://db:8000"]
        write_yaml_file(compose, doc)

        mount_tool_artifact_refs_into_runner(compose, "runner", _world(tmp_path))

        assert yaml.safe_load(compose.read_text())["services"]["runner"]["environment"] == [
            "DB_SERVICE_URL=http://db:8000",
            f"{TOOL_ARTIFACT_REFS_DIR_ENV_VAR}={TOOL_ARTIFACT_REFS_MOUNT_PATH}",
        ]

    def test_a_service_declaring_the_variable_is_refused(self, tmp_path: Path) -> None:
        compose = tmp_path / "environment.compose.yaml"
        doc = json.loads(json.dumps(_RUNNER_COMPOSE))
        doc["services"]["runner"]["environment"] = {TOOL_ARTIFACT_REFS_DIR_ENV_VAR: "/elsewhere"}
        write_yaml_file(compose, doc)

        with pytest.raises(ValueError, match="which the engine sets"):
            mount_tool_artifact_refs_into_runner(compose, "runner", _world(tmp_path))

    @pytest.mark.parametrize(
        "volume",
        [
            pytest.param(f"./data:{TOOL_ARTIFACT_REFS_MOUNT_PATH}:ro", id="short"),
            pytest.param(
                {"type": "bind", "source": "./data", "target": TOOL_ARTIFACT_REFS_MOUNT_PATH},
                id="long",
            ),
        ],
    )
    def test_a_volume_at_the_mount_path_is_refused(
        self, tmp_path: Path, volume: str | dict[str, str]
    ) -> None:
        compose = tmp_path / "environment.compose.yaml"
        doc = json.loads(json.dumps(_RUNNER_COMPOSE))
        doc["services"]["runner"]["volumes"] = [volume]
        write_yaml_file(compose, doc)

        with pytest.raises(ValueError, match="already mounts a volume"):
            mount_tool_artifact_refs_into_runner(compose, "runner", _world(tmp_path))

    def test_a_root_that_is_not_a_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not an absolute directory"):
            mount_tool_artifact_refs_into_runner(_compose(tmp_path), "runner", tmp_path / "absent")


# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


class TestComposer:
    def _spec(self, compose: Path, root: Path) -> Any:
        from tests.unit.test_default_substrate_composer import _manifest, _trial_spec

        spec = _trial_spec(_manifest(compose))
        spec.task = spec.task.model_copy(
            update={
                "tool_artifact_refs": {
                    "tools/world_tool.py": _ref_for(root, "tools/world_tool.py")
                },
                "trial_data_delivery": "mount",
            }
        )
        return spec

    def test_the_trial_runner_stack_gets_the_root_and_no_other_stack_does(
        self, tmp_path: Path
    ) -> None:
        from tests.unit.test_default_substrate_composer import (
            _always_ready_loader,
            _decl,
            _empty_run_sub,
            _fake_client_factory,
            _FakeMaterialiser,
        )
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        compose = _compose(tmp_path)
        root = _world(tmp_path)
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        plan = [
            _decl(compose, stack_id="world", stack_scope="trial", runner_service="runner"),
            _decl(compose, stack_id="side", stack_scope="trial", runner_service=None),
        ]

        asked: list[str] = []

        def task_dir_of(task_id: str) -> Path:
            asked.append(task_id)
            return root

        run_sub = dataclasses.replace(_empty_run_sub(), task_dir_of=task_dir_of)
        spec = self._spec(compose, root)
        composer.provision_trial(plan, spec, run_sub)

        assert [ctx.tool_artifact_refs_root for ctx in materialiser.materialise_contexts] == [
            root.resolve(),
            None,
        ]
        assert asked == [spec.task.task_id]

    def test_references_with_no_way_to_find_the_task_are_refused(self, tmp_path: Path) -> None:
        from tests.unit.test_default_substrate_composer import (
            _always_ready_loader,
            _decl,
            _empty_run_sub,
            _fake_client_factory,
            _FakeMaterialiser,
        )
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        compose = _compose(tmp_path)
        root = _world(tmp_path)
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        plan = [_decl(compose, stack_id="world", stack_scope="trial", runner_service="runner")]

        with pytest.raises(ProvisionError, match="no way to locate the task's directory"):
            composer.provision_trial(plan, self._spec(compose, root), _empty_run_sub())

    def test_a_run_scope_runner_is_refused_before_any_stack_starts(self, tmp_path: Path) -> None:
        from tests.unit.test_default_substrate_composer import (
            _always_ready_loader,
            _decl,
            _empty_run_sub,
            _fake_client_factory,
            _FakeMaterialiser,
        )
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        compose = _compose(tmp_path)
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        plan = [
            _decl(compose, stack_id="shared", stack_scope="run", runner_service="runner"),
            _decl(compose, stack_id="side", stack_scope="trial", runner_service=None),
        ]

        with pytest.raises(ProvisionError, match="needs a trial-scope stack to own the runner"):
            composer.provision_trial(plan, self._spec(compose, _world(tmp_path)), _empty_run_sub())
        assert materialiser.materialise_contexts == []


# ---------------------------------------------------------------------------
# Grading paths without the mount refuse references
# ---------------------------------------------------------------------------


def _refs_description_dict(tmp_path: Path) -> dict[str, Any]:
    """A description with references, of a task whose own trial-scope stack owns the runner."""
    from tolokaforge.core.models import StackPatch
    from tolokaforge.core.project_loader import resolve as resolve_environment

    manifest = resolve_environment(
        None,
        EnvironmentPatch(
            stack=StackPatch(compose_file=_compose(tmp_path), runner_service="runner")
        ),
    )
    assert manifest is not None
    return {
        **_description_with_refs(_world(tmp_path)),
        "environment_manifest": manifest.model_dump(mode="json"),
    }


class TestGradersWithoutTheMount:
    def test_the_detached_grader_refuses(self, tmp_path: Path) -> None:
        grading = RunnerGradingConfig(
            combine_method="weighted", weights={"state_checks": 1.0}, pass_threshold=0.5
        )
        dispatch = GradeDispatch(
            trial_id="refs_task:0",
            llm_messages_json="[]",
            termination_reason="",
            task_config_json=grading.model_dump_json(),
            judge_model_config_json="",
            task_description_json=json.dumps(_refs_description_dict(tmp_path)),
            runner_substrate_address="localhost:1",
        )

        with pytest.raises(GradingFailedError, match="cannot read tool artefacts delivered"):
            GraderCompositeDispatch(logging.getLogger("refs")).grade(dispatch)  # type: ignore[arg-type]

    def test_the_offline_composite_kind_refuses(self, tmp_path: Path) -> None:
        from tests.unit.grading.kinds.test_composite_grader_kind import _OfflineV11Substrate

        grading = RunnerGradingConfig(
            combine_method="weighted", weights={"state_checks": 1.0}, pass_threshold=0.5
        )
        substrate = _OfflineV11Substrate(task_description=_refs_description_dict(tmp_path))

        with pytest.raises(GraderKindRefusedError, match="delivered by reference"):
            CompositeGraderKind().evaluate(
                substrate=substrate,  # type: ignore[arg-type]
                task_config=grading,
                kind_config=None,
                trial_id="refs_task:0",
                agent_tools={},
                logger=logging.getLogger("refs"),  # type: ignore[arg-type]
            )

    def test_the_snapshot_bundle_producer_refuses(self, tmp_path: Path) -> None:
        class _Substrate:
            def filesystem_root(self) -> Path:
                return tmp_path

        description = TaskDescription.model_validate(_refs_description_dict(tmp_path))

        with pytest.raises(ValueError, match="carries inline artefacts only"):
            serialize_bundle_from_substrate(
                substrate=_Substrate(),
                trial_id="refs_task:0",
                out_dir=tmp_path / "bundle",
                trajectory=None,
                task_description=description,
            )


def test_the_docker_materialiser_writes_the_mount_into_the_trial_compose(tmp_path: Path) -> None:
    """The context's root reaches the materialised compose file of the runner's stack."""
    from tests.canonical._docker_compose_stubs import InertDockerCompose
    from tolokaforge.core.composition_runtime import MaterialiseContext
    from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser
    from tolokaforge.core.run_display_events import _NULL_EVENTS
    from tolokaforge.core.trial import NetworkPolicy
    from tolokaforge.runner.models import StackDecl

    compose = _compose(tmp_path / "stack")
    root = _world(tmp_path)
    materialiser = DockerComposeMaterialiser(docker_compose_factory=InertDockerCompose)
    ctx = MaterialiseContext(
        scope_key="refs_task:0",
        stack_id="world",
        network_policy=NetworkPolicy.NO_INTERNET,
        limited_internet_allowlist=(),
        restricted_services=frozenset(),
        mount_docker_socket=False,
        expose_substrate=False,
        log_capture=None,
        write_compose_env=None,
        events=_NULL_EVENTS,
        component_id_prefix="trial/refs_task:0",
        tool_artifact_refs_root=root,
    )
    decl = StackDecl(
        stack_id="world", compose_file=compose, stack_scope="trial", runner_service="runner"
    )

    handle = materialiser.materialise(decl, ctx)
    try:
        written = yaml.safe_load((handle.temp_dir / compose.name).read_text())  # type: ignore[attr-defined]
        assert {
            "type": "bind",
            "source": str(root),
            "target": TOOL_ARTIFACT_REFS_MOUNT_PATH,
            "read_only": True,
        } in written["services"]["runner"]["volumes"]
    finally:
        materialiser.teardown(handle)


class TestRunStartGate:
    """The orchestrator refuses, before any trial, a grader that cannot read the mount."""

    def _orchestrator(self, tmp_path: Path, grader: dict[str, Any] | None) -> Any:
        from types import SimpleNamespace

        description = TaskDescription.model_validate(_refs_description_dict(tmp_path))
        grader_config = None
        if grader is not None:
            grader_config = SimpleNamespace(
                name=grader.get("name"),
                snapshot=SimpleNamespace(enabled=grader.get("snapshot", False)),
            )
        return SimpleNamespace(
            config=SimpleNamespace(grader=grader_config),
            adapter=SimpleNamespace(trial_grader_name="runner_rpc"),
            tasks=[SimpleNamespace(task_id="refs_task")],
            _task_description=lambda task_id: description,
        )

    @pytest.mark.parametrize(
        ("grader", "fragment"),
        [
            ({"name": "grader_rpc"}, "grader 'grader_rpc'"),
            ({"name": "queue"}, "grader 'queue'"),
            ({"name": "runner_rpc", "snapshot": True}, "grader.snapshot.enabled=true"),
        ],
    )
    def test_a_grader_off_the_runner_is_refused(
        self, tmp_path: Path, grader: dict[str, Any], fragment: str
    ) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        with pytest.raises(ValueError, match=fragment) as excinfo:
            Orchestrator._refuse_tool_artifact_refs_the_grader_cannot_read(
                self._orchestrator(tmp_path, grader)
            )
        assert "refs_task" in str(excinfo.value)

    @pytest.mark.parametrize("grader", [None, {"name": "runner_rpc"}])
    def test_the_runner_grader_passes(self, tmp_path: Path, grader: dict[str, Any] | None) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        Orchestrator._refuse_tool_artifact_refs_the_grader_cannot_read(
            self._orchestrator(tmp_path, grader)
        )


class TestTrialContractGate:
    """The library route applies the same rule to the one trial it is handed (ADR-0072)."""

    def _inputs(self, tmp_path: Path, grader: dict[str, Any] | None) -> Any:
        from types import SimpleNamespace

        description = TaskDescription.model_validate(_refs_description_dict(tmp_path))
        grader_config = None
        if grader is not None:
            grader_config = SimpleNamespace(
                name=grader.get("name"),
                snapshot=SimpleNamespace(enabled=grader.get("snapshot", False)),
            )
        return SimpleNamespace(
            grader_config=grader_config, trial_spec=SimpleNamespace(task=description)
        )

    @pytest.mark.parametrize(
        ("grader", "default", "fragment"),
        [
            ({"name": "grader_rpc"}, "runner_rpc", "grader 'grader_rpc'"),
            (None, "queue", "grader 'queue'"),
            ({"name": "runner_rpc", "snapshot": True}, "runner_rpc", "grader.snapshot.enabled"),
        ],
    )
    def test_a_grader_off_the_runner_is_refused(
        self, tmp_path: Path, grader: dict[str, Any] | None, default: str, fragment: str
    ) -> None:
        from tolokaforge.core.trial_validation import validate_trial_inputs

        with pytest.raises(ValueError, match=fragment):
            validate_trial_inputs(self._inputs(tmp_path, grader), default_grader_name=default)

    def test_the_runner_grader_passes(self, tmp_path: Path) -> None:
        from tolokaforge.core.trial_validation import validate_trial_inputs

        validate_trial_inputs(self._inputs(tmp_path, None), default_grader_name="runner_rpc")
