"""A task's initial tables delivered by reference instead of inline (ADR-0071).

A task of a run with ``compute.trial_data_delivery: mount``, whose runner a
trial-scope stack owns, is described without its state file being
parsed: the native adapter delivers the file as one of the trial's referenced
tool artefacts and names it in ``initial_state.tables_ref``. The runner seeds the
database service from the file's bytes, streamed, reads the tables at
registration only when a check or tool reads rows there, and reads them again
for grading only when a grader asks, holding them for one grade. Every grader
sees the tables the inline delivery would have carried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tests.canonical._factories import write_yaml_file
from tests.utils.mock_clients import MockAsyncClient
from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.adapters._task_loader import seeded_tables_from_task
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.compose_materialisation import mount_tool_artifact_refs_into_runner
from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.grading.tool_artifacts import (
    TOOL_ARTIFACT_REFS_DIR_ENV_VAR,
    TOOL_ARTIFACT_REFS_MOUNT_PATH,
    file_sha256,
)
from tolokaforge.core.models import EnvironmentPatch, InitialStateConfig, TaskConfig
from tolokaforge.core.search.backend import SearchIndexBuildError
from tolokaforge.env.json_db_service import app as db_app_module
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError
from tolokaforge.runner.db_client import ValidationError as DBValidationError
from tolokaforge.runner.initial_state_file import (
    InitialStateFile,
    InitialStateFileError,
    seeded_records,
    seeded_tables,
)
from tolokaforge.runner.models import (
    ArtifactRef,
    InitialStateTablesRef,
    RunnerInitialStateConfig,
    TaskDescription,
    provisions_database,
)
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION

pytestmark = pytest.mark.unit


_RUNNER_COMPOSE = {
    "services": {
        "runner": {"image": "tolokaforge-runner:local", "ports": ["50051"]},
        "db-service": {"image": "tolokaforge-db-service:local", "ports": ["8000"]},
    }
}

#: A state written in every shape a collection may take: a list of records, a
#: mapping of records keyed by id, a single record, and an empty table.
_STATE = {
    "orders": [{"id": "o1", "total": 12.5, "lines": [{"sku": "a", "qty": 2}]}],
    "customers": {"c1": {"id": "c1", "name": "Ada"}, "c2": {"id": "c2", "name": "Lin"}},
    "settings": {"currency": "EUR", "tax": 0.2},
    "refunds": [],
}


def _write_state(path: Path, state: dict[str, Any] = _STATE) -> None:
    path.write_text(json.dumps(state))


def _ref(root: Path, rel: str) -> ArtifactRef:
    sha256, size = file_sha256(root / rel)
    return ArtifactRef(uri=f"pack:{rel}", sha256=sha256, size=size)


def _tables_ref(root: Path, artifact: str, source: str | None = None) -> InitialStateTablesRef:
    sha256, size = file_sha256(root / (source or artifact))
    return InitialStateTablesRef(artifact=artifact, sha256=sha256, size=size)


def _description_dict(
    root: Path,
    *,
    state_rel: str = "initial_state.json",
    agent_tools: list[dict[str, Any]] | None = None,
    state_checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refs = {state_rel: _ref(root, state_rel)}
    description: dict[str, Any] = {
        "task_id": "initial_ref_task",
        "name": "initial ref task",
        "category": "test",
        "description": "a task delivering its initial tables by reference",
        "adapter_type": "native",
        "system_prompt": "",
        "agent_tools": agent_tools or [],
        "initial_state": {"tables_ref": _tables_ref(root, state_rel).model_dump(mode="json")},
        "tool_artifact_refs": {target: ref.model_dump(mode="json") for target, ref in refs.items()},
    }
    if state_checks is not None:
        description["grading"] = {"state_checks": state_checks}
    return description


def _world(tmp_path: Path, state: dict[str, Any] = _STATE) -> Path:
    root = tmp_path / "world"
    root.mkdir()
    _write_state(root / "initial_state.json", state)
    return root


# ---------------------------------------------------------------------------
# The wire model
# ---------------------------------------------------------------------------


class TestWireModel:
    def test_tables_travel_inline_or_by_reference_never_both(self, tmp_path: Path) -> None:
        ref = InitialStateTablesRef(artifact="initial_state.json", sha256="0" * 64, size=2)
        with pytest.raises(ValidationError, match="both inline and by reference"):
            RunnerInitialStateConfig(tables={"orders": []}, tables_ref=ref)

    def test_the_reference_names_a_referenced_artifact_with_its_digest(
        self, tmp_path: Path
    ) -> None:
        root = _world(tmp_path)
        description = _description_dict(root)
        assert TaskDescription.model_validate(description).initial_state.tables_ref is not None

        unreferenced = dict(description, tool_artifact_refs=None)
        with pytest.raises(ValidationError, match="does not reference"):
            TaskDescription.model_validate(unreferenced)

        stale = json.loads(json.dumps(description))
        stale["initial_state"]["tables_ref"]["size"] += 1
        with pytest.raises(ValidationError, match="its tool artefact reference"):
            TaskDescription.model_validate(stale)

    def test_the_reference_is_left_out_of_the_dump_while_absent(self) -> None:
        assert "tables_ref" not in RunnerInitialStateConfig().model_dump(mode="json")

    def test_a_reference_provisions_a_database_without_being_read(self, tmp_path: Path) -> None:
        description = TaskDescription.model_validate(_description_dict(_world(tmp_path)))
        assert description.initial_state.tables == {}
        assert provisions_database(description.initial_state) is True

    def test_the_protocol_version_is_the_one_that_carries_the_reference(self) -> None:
        assert ENGINE_PROTOCOL_VERSION >= 10


# ---------------------------------------------------------------------------
# The environment resolver
# ---------------------------------------------------------------------------


def _compose(directory: Path) -> Path:
    compose = directory / "environment.compose.yaml"
    write_yaml_file(compose, _RUNNER_COMPOSE)
    return compose


class TestDeliveryIsARunSetting:
    def test_a_task_manifest_no_longer_names_a_delivery(self) -> None:
        with pytest.raises(ValidationError, match="compute.trial_data_delivery"):
            EnvironmentPatch.model_validate({"initial_state_delivery": "mount"})


# ---------------------------------------------------------------------------
# The native adapter
# ---------------------------------------------------------------------------


def _pack(
    tmp_path: Path,
    *,
    initial: str | None,
    layout: str = "root",
    state_text: str | None = None,
    grading: dict[str, Any] | None = None,
    mcp: bool = True,
) -> NativeAdapter:
    """A native pack whose state file is ``initial_state.json``.

    ``root``: the MCP server and the state file side by side at the pack root, as a
    pack whose server reads ``initial_state.json`` beside itself. ``shared``: the
    server under ``_shared/`` and the case's own state file, which the adapter
    copies beside the server.
    """
    task_dir = tmp_path / "tasks" / "ref_pack"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "fixtures").mkdir()
    (task_dir / "fixtures" / "tools.json").write_text(json.dumps([]))
    write_yaml_file(task_dir / "environment.compose.yaml", _RUNNER_COMPOSE)
    if layout == "root":
        server, state = "server.py", "initial_state.json"
    else:
        server, state = "_shared/mcp_server.py", "case_state.json"
        (task_dir / "_shared").mkdir()
    if mcp:
        (task_dir / server).write_text("# stub server\n")
    if state_text is None:
        _write_state(task_dir / state)
    else:
        (task_dir / state).write_text(state_text)
    environment: dict[str, Any] = {
        "stack": {"compose_file": "environment.compose.yaml", "runner_service": "runner"},
    }
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "ref_pack",
            "name": "ref_pack",
            "category": "tool_use",
            "description": "a pack delivering its initial state by reference",
            "initial_state": {"json_db": state},
            "tools": {
                "agent": {"mcp_server": server, "enabled": []} if mcp else {"enabled": []},
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
        grading
        or {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )
    params: dict[str, Any] = {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
    if initial is not None:
        params["trial_data_delivery"] = initial
    return NativeAdapter(params)


def _task_dir(tmp_path: Path) -> Path:
    return (tmp_path / "tasks" / "ref_pack").resolve()


class TestNativeAdapter:
    def test_the_state_file_is_the_artifact_the_pack_already_carries(self, tmp_path: Path) -> None:
        description = _pack(tmp_path, initial="mount").to_task_description("ref_pack")

        ref = description.initial_state.tables_ref
        assert description.initial_state.tables == {}
        assert ref is not None and ref.artifact == "initial_state.json"
        refs = description.tool_artifact_refs
        assert refs is not None and refs[ref.artifact].uri == "pack:initial_state.json"
        content = (_task_dir(tmp_path) / "initial_state.json").read_bytes()
        assert ref.sha256 == hashlib.sha256(content).hexdigest() == refs[ref.artifact].sha256
        assert ref.size == len(content)

    def test_the_case_state_beside_a_shared_server_is_named_once(self, tmp_path: Path) -> None:
        description = _pack(tmp_path, initial="mount", layout="shared").to_task_description(
            "ref_pack"
        )

        ref = description.initial_state.tables_ref
        refs = description.tool_artifact_refs
        assert ref is not None and refs is not None
        assert refs[ref.artifact].uri == "pack:case_state.json"
        uris = [entry.uri for entry in refs.values()]
        assert uris.count("pack:case_state.json") == len(
            [t for t, e in refs.items() if e.uri == "pack:case_state.json"]
        )

    def test_a_task_with_no_bundle_gets_its_state_file_referenced(self, tmp_path: Path) -> None:
        description = _pack(tmp_path, initial="mount", mcp=False).to_task_description("ref_pack")

        refs = description.tool_artifact_refs
        assert refs is not None and set(refs) == {"initial_state.json"}
        assert description.initial_state.tables_ref is not None

    def test_the_host_never_parses_the_file(self, tmp_path: Path) -> None:
        adapter = _pack(tmp_path, initial="mount", state_text="{not json")

        description = adapter.to_task_description("ref_pack")
        environment = adapter.create_environment("ref_pack")

        assert description.initial_state.tables_ref is not None
        assert environment.data == {}

    def test_inline_parses_the_file_as_before(self, tmp_path: Path) -> None:
        mounted = _pack(tmp_path / "mount", initial="mount").to_task_description("ref_pack")
        inline = _pack(tmp_path / "inline", initial=None).to_task_description("ref_pack")

        assert inline.initial_state.tables_ref is None
        assert inline.initial_state.tables == seeded_tables(_STATE)
        assert inline.tool_artifact_refs is None
        # The two descriptions carry the same files, by reference or inline.
        assert mounted.tool_artifact_refs is not None
        assert set(mounted.tool_artifact_refs) == set(inline.tool_artifacts)

    def test_row_checks_move_to_the_runner(self, tmp_path: Path) -> None:
        grading = {
            "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
            "state_checks": {"id_fields": {"no_such_table": "id"}, "jsonpaths": []},
        }
        with pytest.raises(ValueError, match="no_such_table"):
            _pack(tmp_path / "inline", initial=None, grading=grading).to_task_description(
                "ref_pack"
            )

        description = _pack(
            tmp_path / "mount", initial="mount", grading=grading
        ).to_task_description("ref_pack")
        assert description.grading.state_checks is not None
        assert description.grading.state_checks.id_fields == {"no_such_table": "id"}

    def test_a_state_file_outside_the_task_directory_is_refused(self, tmp_path: Path) -> None:
        adapter = _pack(tmp_path, initial="mount", mcp=False)
        task_yaml = _task_dir(tmp_path) / "task.yaml"
        outside = tmp_path / "elsewhere.json"
        _write_state(outside)
        task_yaml.write_text(
            task_yaml.read_text().replace(
                "json_db: initial_state.json", "json_db: ../../elsewhere.json"
            )
        )
        adapter = NativeAdapter(
            {
                "base_dir": str(tmp_path),
                "tasks_glob": "tasks/**/task.yaml",
                "trial_data_delivery": "mount",
            }
        )

        with pytest.raises(ValueError, match="reads the state file from the task"):
            adapter.to_task_description("ref_pack")

    def test_the_grading_gate_reads_the_file_only_when_a_rule_asks(self, tmp_path: Path) -> None:
        adapter = _pack(tmp_path, initial="mount", state_text="{not json")
        task = adapter.get_task("ref_pack")

        layer = NativeAdapter.grading_seeded_tables(task, _task_dir(tmp_path))
        assert layer.known
        with pytest.raises(json.JSONDecodeError):
            dict(layer.tables or {})

        _write_state(_task_dir(tmp_path) / "initial_state.json")
        layer = NativeAdapter.grading_seeded_tables(task, _task_dir(tmp_path))
        assert dict(layer.tables or {}) == seeded_tables_from_task(task, _task_dir(tmp_path))
        assert dict(layer.table_shapes) == {
            "customers": "a mapping of records keyed by id",
            "settings": "a single record (a mapping)",
        }


# ---------------------------------------------------------------------------
# The engine host's trial setup
# ---------------------------------------------------------------------------


class TestEngineHost:
    def test_hydrate_without_the_seed_reads_nothing(self, tmp_path: Path) -> None:
        state = EnvironmentState(tmp_path, InitialStateConfig(json_db="missing.json"))
        state.hydrate(seed_db=False)
        assert state.db_state == {} and state.initial_db_state == {}

        with pytest.raises(FileNotFoundError):
            EnvironmentState(tmp_path, InitialStateConfig(json_db="missing.json")).hydrate()

    def test_the_conductor_keeps_no_copy_of_a_referenced_state(self, tmp_path: Path) -> None:
        from tests.unit.test_conductor import _conductor_registering, _make_spec, _register_result

        root = _world(tmp_path)
        spec = _make_spec()
        spec.task = TaskDescription.model_validate(
            {**_description_dict(root), "task_id": spec.task.task_id, "name": spec.task.name}
        )
        conductor = _conductor_registering(tmp_path, _register_result([], []))
        # The task names a state file the host could not read: a host that read it fails.
        task = TaskConfig(task_id="t1", description="d", initial_state={"json_db": "absent.json"})

        setup = conductor._setup_trial(spec, task)

        assert setup.env_state.db_state == {}
        assert setup.env_state.initial_db_state == {}


# ---------------------------------------------------------------------------
# The compose mount
# ---------------------------------------------------------------------------


def test_the_state_file_rides_the_artifact_mount(tmp_path: Path) -> None:
    description = _pack(tmp_path, initial="mount").to_task_description("ref_pack")
    refs = description.tool_artifact_refs
    assert refs is not None
    compose = _compose(tmp_path)

    task_dir = _task_dir(tmp_path)
    assert description.initial_state.tables_ref is not None
    assert refs[description.initial_state.tables_ref.artifact].uri == "pack:initial_state.json"

    mount_tool_artifact_refs_into_runner(compose, "runner", task_dir)

    import yaml

    volumes = yaml.safe_load(compose.read_text())["services"]["runner"]["volumes"]
    assert volumes == [
        {
            "type": "bind",
            "source": str(task_dir),
            "target": TOOL_ARTIFACT_REFS_MOUNT_PATH,
            "read_only": True,
        }
    ]


# ---------------------------------------------------------------------------
# The file the runner reads
# ---------------------------------------------------------------------------


class TestInitialStateFile:
    def _file(self, tmp_path: Path, state: dict[str, Any] = _STATE) -> InitialStateFile:
        path = tmp_path / "initial_state.json"
        _write_state(path, state)
        sha256, size = file_sha256(path)
        return InitialStateFile(path=path, sha256=sha256, size=size)

    def test_the_tables_are_the_ones_the_inline_delivery_carries(self, tmp_path: Path) -> None:
        tables = self._file(tmp_path).read_tables()
        assert tables == seeded_tables(_STATE)
        assert list(tables) == list(_STATE)
        assert tables["customers"] == [{"id": "c1", "name": "Ada"}, {"id": "c2", "name": "Lin"}]
        assert tables["settings"] == [{"currency": "EUR", "tax": 0.2}]

    def test_a_changed_file_is_refused_at_every_read(self, tmp_path: Path) -> None:
        state_file = self._file(tmp_path)
        _write_state(state_file.path, {"orders": []})

        with pytest.raises(InitialStateFileError, match="changed after the trial was registered"):
            state_file.read_tables()
        with pytest.raises(InitialStateFileError, match="changed after"):
            b"".join(state_file.iter_chunks())

    def test_a_collection_that_is_not_records_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InitialStateFileError, match="not all mappings"):
            self._file(tmp_path, {"versions": [1, 2]}).read_tables()

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        missing = InitialStateFile(path=tmp_path / "absent.json", sha256="0" * 64, size=0)
        with pytest.raises(InitialStateFileError, match="cannot be read"):
            missing.read_tables()

    def test_the_chunks_are_the_files_bytes(self, tmp_path: Path) -> None:
        state_file = self._file(tmp_path)
        assert b"".join(state_file.iter_chunks()) == state_file.path.read_bytes()

    def test_non_finite_numbers_read_as_null_as_the_inline_serialiser_writes_them(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "initial_state.json"
        path.write_text(
            '{"m": [{"id": 1, "a": NaN, "b": Infinity, "c": -Infinity, "d": 1e999, "e": -1e999, "f": 1.5}]}'
        )
        sha256, size = file_sha256(path)

        tables = InitialStateFile(path=path, sha256=sha256, size=size).read_tables()

        assert tables == {
            "m": [{"id": 1, "a": None, "b": None, "c": None, "d": None, "e": None, "f": 1.5}]
        }


def test_the_db_services_vendored_normaliser_is_the_engines() -> None:
    samples: list[Any] = [
        [],
        [{"id": 1}],
        {},
        {"a": {"id": 1}, "b": {"id": 2}},
        {"a": {"id": 1}, "b": 2},
        {"x": 1},
        3,
        "s",
        None,
    ]
    for sample in samples:
        assert db_app_module.seeded_records(sample) == seeded_records(sample), sample


# ---------------------------------------------------------------------------
# The db-service route and the client
# ---------------------------------------------------------------------------


class TestSeedingFromTheFile:
    def test_the_route_seeds_the_tables_the_inline_init_would(
        self, db_client: DBServiceClient, tmp_path: Path
    ) -> None:
        state_file = TestInitialStateFile()._file(tmp_path)
        trial_id = "initial_ref_route:0"

        async def seed_and_read() -> Any:
            await db_client.init_trial_from_json_db(
                trial_id,
                state_file.aiter_chunks(),
                schemas=[],
                unstable_fields=[{"table_name": "orders", "field_name": "total"}],
            )
            try:
                return await db_client.get_state(trial_id)
            finally:
                await db_client.delete_trial(trial_id)

        state = asyncio.run(seed_and_read())
        assert state.data == seeded_tables(_STATE)

    def test_records_that_are_not_mappings_are_refused(
        self, db_client: DBServiceClient, tmp_path: Path
    ) -> None:
        state_file = TestInitialStateFile()._file(tmp_path, {"version": 3})

        with pytest.raises(DBValidationError, match="version"):
            asyncio.run(
                db_client.init_trial_from_json_db("initial_ref_bad:0", state_file.aiter_chunks())
            )

    def test_a_file_that_closes_the_object_early_cannot_add_init_fields(
        self, db_client: DBServiceClient, tmp_path: Path
    ) -> None:
        path = tmp_path / "initial_state.json"
        path.write_text(
            '{"t": [{"id": 1}]}, "unstable_fields": [{"table_name": "t", "field_name": "x"}]'
        )
        sha256, size = file_sha256(path)
        state_file = InitialStateFile(path=path, sha256=sha256, size=size)

        with pytest.raises(DBValidationError, match="more than one JSON value"):
            asyncio.run(
                db_client.init_trial_from_json_db(
                    "initial_ref_inject:0", state_file.aiter_chunks(), unstable_fields=[]
                )
            )

    def test_non_finite_numbers_seed_as_null(
        self, db_client: DBServiceClient, tmp_path: Path
    ) -> None:
        path = tmp_path / "initial_state.json"
        path.write_text('{"m": [{"id": 1, "a": NaN, "b": 1e999, "c": -1e999, "d": 1.5}]}')
        sha256, size = file_sha256(path)
        state_file = InitialStateFile(path=path, sha256=sha256, size=size)
        trial_id = "initial_ref_nan:0"

        async def seed_and_read() -> Any:
            await db_client.init_trial_from_json_db(trial_id, state_file.aiter_chunks())
            try:
                return await db_client.get_state(trial_id)
            finally:
                await db_client.delete_trial(trial_id)

        assert asyncio.run(seed_and_read()).data == {
            "m": [{"id": 1, "a": None, "b": None, "c": None, "d": 1.5}]
        }

    def test_a_service_without_the_route_answers_none(self, db_test_client: Any) -> None:
        client = DBServiceClient("http://testserver")
        client.set_test_client(_ServiceWithoutTheRoute(db_test_client, "http://testserver"))

        async def chunks() -> Any:
            raise AssertionError("the file must not be sent to a service without the route")
            yield b""  # pragma: no cover

        assert asyncio.run(client.init_trial_from_json_db("old:0", chunks())) is None

    def test_the_body_streams_over_http(self, db_service_loopback_url: str, tmp_path: Path) -> None:
        state_file = TestInitialStateFile()._file(tmp_path)
        client = DBServiceClient(db_service_loopback_url)
        trial_id = "initial_ref_loopback:0"

        async def seed_and_read() -> Any:
            response = await client.init_trial_from_json_db(trial_id, state_file.aiter_chunks())
            try:
                return response, await client.get_state(trial_id)
            finally:
                await client.delete_trial(trial_id)

        response, state = asyncio.run(seed_and_read())
        assert response is not None and response.tables_initialized == list(_STATE)
        assert state.data == seeded_tables(_STATE)

    def test_a_file_rewritten_mid_stream_creates_no_trial(
        self,
        db_service_loopback_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The digest fails only after the last chunk, so nothing it sent may take effect."""
        monkeypatch.setattr("tolokaforge.runner.initial_state_file._CHUNK_BYTES", 8)
        state_file = TestInitialStateFile()._file(tmp_path)
        original = state_file.path.read_bytes()
        client = DBServiceClient(db_service_loopback_url)
        trial_id = "initial_ref_rewritten:0"

        async def rewriting_chunks() -> Any:
            chunks = state_file.aiter_chunks()
            yield await anext(chunks)
            state_file.path.write_bytes(original.replace(b"a", b"b") + b" ")
            async for chunk in chunks:
                yield chunk

        async def seed_and_read() -> Any:
            with pytest.raises(InitialStateFileError, match="changed after"):
                await client.init_trial_from_json_db(trial_id, rewriting_chunks())
            return await client.get_state(trial_id)

        with pytest.raises(DBTrialNotFoundError):
            asyncio.run(seed_and_read())


class _ServiceWithoutTheRoute(MockAsyncClient):
    """A db-service image that predates ``/init/json_db``: FastAPI's bare 404."""

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/init/json_db"):
            return httpx.Response(
                404, json={"detail": "Not Found"}, request=httpx.Request("POST", url)
            )
        return await super().post(url, **kwargs)


# ---------------------------------------------------------------------------
# RegisterTrial and grading on the runner
# ---------------------------------------------------------------------------


def _register(runner_service: Any, context: Any, description: dict[str, Any], trial_id: str):
    return runner_service.RegisterTrial(
        register_request(trial_spec_json(description, trial_id=trial_id), trial_id=trial_id),
        context,
    )


def _db_state(runner_service: Any, trial_id: str) -> dict[str, Any]:
    return runner_service._run_async(runner_service.db_client.get_state(trial_id), timeout=30).data


class _ReadCounter:
    """Counts the reads of every :class:`InitialStateFile`'s tables."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = InitialStateFile.read_tables_and_shapes

        def counted(state_file: InitialStateFile) -> Any:
            self.calls += 1
            return original(state_file)

        monkeypatch.setattr(InitialStateFile, "read_tables_and_shapes", counted)


class TestRegisterTrial:
    def test_the_database_is_seeded_from_the_file_and_nothing_is_held(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        reads = _ReadCounter(monkeypatch)
        trial_id = "initial_ref_task:0"

        response = _register(runner_service, mock_grpc_context, _description_dict(root), trial_id)
        try:
            assert response.success is True, response.error
            context = runner_service.trials[trial_id]
            assert reads.calls == 0  # streamed: the runner parsed nothing to seed it
            assert context.task_description.initial_state.tables == {}
            assert context._initial_tables_held is None
            assert _db_state(runner_service, trial_id) == seeded_tables(_STATE)

            # Grading reads the file on first use, once per grade, and drops it after.
            with context.holding_initial_tables():
                first = context.initial_tables()
                assert context.initial_tables() is first
            assert reads.calls == 1
            assert first == seeded_tables(_STATE)
            assert context._initial_tables_held is None
        finally:
            runner_service._run_async(runner_service.cleanup_trial(trial_id), timeout=30)

    def test_a_db_service_without_the_route_is_seeded_inline(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        db_test_client: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        runner_service.db_client.set_test_client(
            _ServiceWithoutTheRoute(db_test_client, "http://testserver")
        )
        trial_id = "initial_ref_task:1"

        response = _register(runner_service, mock_grpc_context, _description_dict(root), trial_id)
        try:
            assert response.success is True, response.error
            assert _db_state(runner_service, trial_id) == seeded_tables(_STATE)
        finally:
            runner_service._run_async(runner_service.cleanup_trial(trial_id), timeout=30)

    def test_a_file_changed_after_description_is_refused(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        description = _description_dict(root)
        _write_state(root / "initial_state.json", {"orders": []})
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "initial_ref_task:2"

        response = _register(runner_service, mock_grpc_context, description, trial_id)

        assert response.success is False
        assert "initial_state.json" in response.error and "do not resolve" in response.error
        assert trial_id not in runner_service.trials
        assert trial_id not in runner_service._artifact_dirs

    def test_a_failed_db_init_leaves_nothing_behind(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "initial_ref_task:init_fails"
        extracted: list[Path] = []
        extract = runner_service._extract_tool_artifacts

        def recording(*args: Any, **kwargs: Any) -> Path:
            extracted.append(extract(*args, **kwargs))
            return extracted[-1]

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the service refused the state")

        monkeypatch.setattr(runner_service, "_extract_tool_artifacts", recording)
        monkeypatch.setattr(runner_service.db_client, "init_trial_from_json_db", refuse)

        response = _register(runner_service, mock_grpc_context, _description_dict(root), trial_id)

        assert response.success is False
        assert "the service refused the state" in response.error
        assert trial_id not in runner_service._artifact_dirs
        assert extracted and not extracted[0].exists()

    def test_a_failure_after_the_seeding_leaves_nothing_behind(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "initial_ref_task:index_fails"

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise SearchIndexBuildError("no client for the backend")

        monkeypatch.setattr(runner_service, "_build_search_index", refuse)

        response = _register(runner_service, mock_grpc_context, _description_dict(root), trial_id)

        assert response.success is False
        assert trial_id not in runner_service._artifact_dirs
        with pytest.raises(DBTrialNotFoundError):
            _db_state(runner_service, trial_id)

    def test_row_checks_read_the_file_at_registration(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        reads = _ReadCounter(monkeypatch)

        refused = _register(
            runner_service,
            mock_grpc_context,
            _description_dict(root, state_checks={"id_fields": {"no_such_table": "id"}}),
            "initial_ref_task:3",
        )
        assert refused.success is False
        assert "no_such_table" in refused.error
        assert "initial_ref_task:3" not in runner_service._artifact_dirs

        trial_id = "initial_ref_task:4"
        registered = _register(
            runner_service,
            mock_grpc_context,
            _description_dict(root, state_checks={"id_fields": {"orders": "id"}}),
            trial_id,
        )
        try:
            assert registered.success is True, registered.error
            assert reads.calls == 2
            assert _db_state(runner_service, trial_id) == seeded_tables(_STATE)
            assert runner_service.trials[trial_id]._initial_tables_held is None
        finally:
            runner_service._run_async(runner_service.cleanup_trial(trial_id), timeout=30)

    def test_json_db_tools_over_an_empty_file_are_refused(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _world(tmp_path, state={})
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        trial_id = "initial_ref_task:5"
        tool = {"name": "db_query", "description": "db_query", "parameters": {"type": "object"}}

        response = _register(
            runner_service, mock_grpc_context, _description_dict(root, agent_tools=[tool]), trial_id
        )

        assert response.success is False
        assert "JSON-DB tools ['db_query']" in response.error
        assert trial_id not in runner_service.trials

    def test_the_grading_substrate_reads_the_file_on_first_use(
        self,
        runner_service: Any,
        mock_grpc_context: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tolokaforge.runner.service import _LiveStateSync

        root = _world(tmp_path)
        monkeypatch.setenv(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, str(root))
        reads = _ReadCounter(monkeypatch)
        trial_id = "initial_ref_task:6"
        assert _register(
            runner_service, mock_grpc_context, _description_dict(root), trial_id
        ).success
        try:
            context = runner_service.trials[trial_id]
            substrate = runner_service._build_grading_substrate(
                trial_id, context, _LiveStateSync(runner_service, trial_id, context)
            )
            assert reads.calls == 0
            assert substrate.initial_state() == seeded_tables(_STATE)
            assert substrate.initial_state() is substrate.initial_state()
            assert reads.calls == 1
            substrate.close()
        finally:
            runner_service._run_async(runner_service.cleanup_trial(trial_id), timeout=30)


def test_the_substrate_takes_an_initial_state_or_a_factory_not_both() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        InProcessGradingSubstrate(
            db_reader=None,  # type: ignore[arg-type]
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            initial_state_factory=dict,
        )


def test_custom_checks_see_the_same_initial_state_either_way(tmp_path: Path) -> None:
    """``context.initial_state`` of a check is the inline tables, or the file's through the substrate."""
    import logging
    from unittest.mock import MagicMock

    from tolokaforge.core.grading.check_runner import InMemoryCheckExecutor
    from tolokaforge.core.grading.composite import grade_custom_checks

    class _Capturing(InMemoryCheckExecutor):
        def run(self, checks_file: Path, task_dir: Path, ctx: Any, config: Any) -> Any:
            self.ctx = ctx
            return super().run(checks_file, task_dir, ctx, config)

    (tmp_path / "checks.py").write_text("")
    root = _world(tmp_path)
    by_reference = TaskDescription.model_validate(_description_dict(root))
    inline = TaskDescription.model_validate(
        {
            **_description_dict(root),
            "initial_state": {"tables": seeded_tables(_STATE)},
            "tool_artifact_refs": None,
        }
    )
    sha256, size = file_sha256(root / "initial_state.json")
    tables = InitialStateFile(
        path=root / "initial_state.json", sha256=sha256, size=size
    ).read_tables

    seen = []
    for description, substrate_state in (
        (inline, {"initial_state": {}}),
        (by_reference, {"initial_state_factory": tables}),
    ):
        executor = _Capturing()
        grade_custom_checks(
            trial_id="initial_ref_task:9",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=InProcessGradingSubstrate(
                db_reader=MagicMock(),
                knowledge_search=None,
                filesystem_root=None,
                final_state={},
                **substrate_state,
            ),
            llm_messages=[],
            task_description=description,
            artifacts_dir=tmp_path,
            check_executor=executor,
            logger=logging.getLogger("test-initial-ref"),  # type: ignore[arg-type]
        )
        seen.append(executor.ctx.initial_state.data)

    assert seen[0] == seen[1] == seeded_tables(_STATE)


def test_an_engine_before_the_reference_is_refused_at_registration(
    runner_service: Any, mock_grpc_context: Any, tmp_path: Path
) -> None:
    description = _description_dict(_world(tmp_path))
    trial_id = "initial_ref_task:10"

    response = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(description, trial_id=trial_id),
            trial_id=trial_id,
            engine_protocol_version=9,
        ),
        mock_grpc_context,
    )

    assert response.success is False
    assert "requires at least 10" in response.error
    assert trial_id not in runner_service.trials
