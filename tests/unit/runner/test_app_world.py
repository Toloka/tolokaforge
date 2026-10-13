"""An application world served over HTTP by a service of the trial's stack (ADR-0062).

The task declares ``initial_state.app_world``; validation refuses a world nothing can
hold alone, the native adapter carries it to the runner, and the runner mints the
trial's credentials, claims the service, loads the world, reads it back for grading
and restores it. These tests drive the real models, the real runner servicer, the
real ``http_request`` tool and the real db-service app against a made-up world service
served over loopback HTTP (:mod:`tests.utils.fake_app_world`).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tests.canonical._factories import write_yaml_file
from tests.utils.fake_app_world import (
    INITIAL_TABLES,
    MCP_WORLD_SERVER,
    FakeAppWorld,
    HeaderRecorder,
)
from tests.utils.loopback_asgi import serve_asgi_on_loopback
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.adapters._task_loader import (
    load_task_yaml,
    replay_world_under_adapter,
    tool_inventory_under_adapter,
    validate_grading_yaml,
)
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.golden_replay import (
    UnbuildableGoldenReplayWorld,
    require_golden_replay_world,
)
from tolokaforge.core.models import EnvironmentPatch, StackPatch
from tolokaforge.core.models.task_config import TaskConfig
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import AppWorldConfig, RunnerInitialStateConfig
from tolokaforge.secrets import get_default
from tolokaforge.secrets.log_filter import PLACEHOLDER, install_global_redactor
from tolokaforge.tools.builtin.http_request import (
    REDACTED_CREDENTIAL,
    HTTPRequestTool,
    unreachable_hosts,
)

pytestmark = pytest.mark.unit

HOST = "helpdesk.vendor.example"
WORLD = {"url": "http://world:8080", "hosts": [HOST], "actors": {"agent": None, "user": "customer"}}


@pytest.fixture(autouse=True)
def isolated_secrets() -> Iterator[None]:
    """Every minted token lands in a manager of this test's own, restored after."""
    with secret_manager_installed({}):
        yield


@pytest.fixture
def world() -> FakeAppWorld:
    return FakeAppWorld()


@pytest.fixture
def world_url(world: FakeAppWorld) -> Iterator[str]:
    with serve_asgi_on_loopback(world.build()) as url:
        yield url


@pytest.fixture
def recorder() -> HeaderRecorder:
    return HeaderRecorder()


@pytest.fixture
def recorder_url(recorder: HeaderRecorder) -> Iterator[str]:
    with serve_asgi_on_loopback(recorder.build()) as url:
        yield url


def _host(url: str) -> str:
    return url.removeprefix("http://")


# -- the wire ---------------------------------------------------------------------------


def test_an_absent_world_stays_off_the_wire_so_an_older_image_accepts_the_pack() -> None:
    assert "app_world" not in RunnerInitialStateConfig().model_dump()
    assert "app_world" not in json.loads(RunnerInitialStateConfig().model_dump_json())
    assert "app_world" not in TaskConfig(task_id="t", description="d").model_dump()["initial_state"]


def test_a_declared_world_round_trips_the_wire_without_a_credential() -> None:
    state = RunnerInitialStateConfig(tables=INITIAL_TABLES, app_world=WORLD)
    dumped = state.model_dump_json()
    again = RunnerInitialStateConfig.model_validate_json(dumped)
    assert again.app_world == AppWorldConfig(**WORLD)
    assert again.app_world.service == "world"
    assert "token" not in dumped.lower()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"url": "world:8080"}, "must be an http"),
        ({"hosts": []}, "hosts is empty"),
        ({"hosts": ["https://helpdesk.vendor.example"]}, "without a scheme or path"),
        ({"actors": {}}, "actors is empty"),
        ({"actors": {"judge": None}}, "agent"),
        ({"admin_token": "literal"}, "Extra inputs"),
    ],
)
def test_a_malformed_world_declaration_is_refused(override: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AppWorldConfig(**{**WORLD, **override})


# -- task.yaml ----------------------------------------------------------------------------


def _task(**overrides: Any) -> dict[str, Any]:
    http = {"enabled": ["http_request"], "http_request": {"allowed_hosts": [HOST]}}
    task = {
        "task_id": "world",
        "description": "an app world over HTTP",
        "initial_state": {"json_db": "initial_state.json", "app_world": WORLD},
        "tools": {"agent": http, "user": http},
        "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
    }
    task.update(overrides)
    return task


def test_a_world_every_actor_reaches_alone_is_accepted() -> None:
    task = TaskConfig.model_validate(_task())
    assert task.initial_state.app_world == AppWorldConfig(**WORLD)


def test_a_world_without_its_tables_is_refused() -> None:
    with pytest.raises(ValidationError, match="json_db is the world as tables"):
        TaskConfig.model_validate(_task(initial_state={"app_world": WORLD}))


@pytest.mark.parametrize("actor", ["agent", "user"])
def test_a_world_beside_an_mcp_server_is_refused(actor: str) -> None:
    task = _task()
    task["tools"][actor] = {**task["tools"][actor], "mcp_server": "mcp_server.py"}
    with pytest.raises(ValidationError, match=f"tools.{actor}.mcp_server would both hold"):
        TaskConfig.model_validate(task)


def test_a_world_host_outside_an_actors_allowed_hosts_is_refused() -> None:
    task = _task()
    task["tools"]["user"] = {"enabled": ["http_request"]}
    with pytest.raises(ValidationError, match=r"not in tools.user.http_request.allowed_hosts"):
        TaskConfig.model_validate(task)


def test_a_world_caller_without_http_request_is_refused() -> None:
    task = _task()
    task["tools"]["user"] = {"enabled": []}
    with pytest.raises(ValidationError, match="tools.user.enabled lists no http_request"):
        TaskConfig.model_validate(task)


# -- isolation, where the task and the project's environment meet --------------------------


def _write_pack(
    tmp_path: Path,
    *,
    services: dict[str, Any] | None,
    project_default_environment: EnvironmentPatch | None = None,
    url: str = WORLD["url"],
) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "world"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text(json.dumps(INITIAL_TABLES))
    write_yaml_file(
        task_dir / "environment.compose.yaml",
        {
            "services": {
                "runner": {"image": "tolokaforge-runner:local", "ports": ["50051"]},
                "world": {"image": "world-service:local", "ports": ["8080"]},
            }
        },
    )
    task = _task(
        initial_state={"json_db": "initial_state.json", "app_world": {**WORLD, "url": url}}
    )
    if services is not None:
        task["environment_manifest"] = {
            "stack": {"compose_file": "./environment.compose.yaml", "runner_service": "runner"},
            "services": services,
        }
    write_yaml_file(task_dir / "task.yaml", task)
    params: dict[str, Any] = {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
    if project_default_environment is not None:
        params["project_default_environment"] = project_default_environment
    return NativeAdapter(params)


@pytest.mark.parametrize("isolation", [None, "ephemeral"])
def test_a_per_trial_world_service_reaches_the_runner(
    tmp_path: Path, isolation: str | None
) -> None:
    services = {} if isolation is None else {"world": {"isolation": isolation}}
    adapter = _write_pack(tmp_path, services=services)

    description = adapter.to_task_description("world")

    assert description.initial_state.app_world == AppWorldConfig(**WORLD)
    assert description.initial_state.tables == INITIAL_TABLES
    assert description.environment_manifest.services["world"].isolation == "ephemeral"


@pytest.mark.parametrize(
    ("isolation", "message"),
    [
        ("shared", "service 'world', whose isolation is 'shared'"),
        ("reset", "service 'world', whose isolation is 'reset'.*admin token the previous trial"),
    ],
)
def test_a_world_service_not_ephemeral_is_refused_by_name(
    tmp_path: Path, isolation: str, message: str
) -> None:
    spec = {"isolation": isolation, "reset": {"seed": "baseline"}} if isolation == "reset" else {}
    adapter = _write_pack(tmp_path, services={"world": {"isolation": isolation, **spec}})
    with pytest.raises(ValueError, match=message):
        adapter.to_task_description("world")


def test_a_world_service_the_project_shares_is_refused(tmp_path: Path) -> None:
    compose = tmp_path / "tasks" / "world" / "environment.compose.yaml"
    project = EnvironmentPatch(
        stack=StackPatch(compose_file=compose, runner_service="runner"),
        services={"world": {"isolation": "shared"}},
    )
    adapter = _write_pack(tmp_path, services=None, project_default_environment=project)
    with pytest.raises(ValueError, match="whose isolation is 'shared'"):
        adapter.to_task_description("world")


def test_a_world_url_naming_no_service_of_the_stack_is_refused(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, services={}, url="http://elsewhere:8080")
    with pytest.raises(ValueError, match="'elsewhere', which is not a service of the task's stack"):
        adapter.to_task_description("world")


def test_a_world_url_with_no_stack_to_run_it_in_is_refused(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, services=None)
    with pytest.raises(ValueError, match="resolves no environment stack"):
        adapter.to_task_description("world")


# -- the authoring gate and the host-side engine ------------------------------------------


def _golden_grading(task_dir: Path) -> Path:
    path = task_dir / "grading.yaml"
    write_yaml_file(
        path,
        {
            "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
            "state_checks": {
                "hash": {
                    "enabled": True,
                    "golden_actions": [
                        {
                            "name": "http_request",
                            "kwargs": {"method": "GET", "url": f"http://{HOST}/api/tickets"},
                        }
                    ],
                }
            },
        },
    )
    return path


def _gate(tmp_path: Path, initial_state: dict[str, Any]):
    write_yaml_file(tmp_path / "task.yaml", _task(initial_state=initial_state))
    (tmp_path / "initial_state.json").write_text(json.dumps(INITIAL_TABLES))
    config, task_dir = load_task_yaml(tmp_path / "task.yaml")
    return validate_grading_yaml(
        _golden_grading(task_dir),
        inventory=tool_inventory_under_adapter(config, task_dir, "native"),
        replay_world=replay_world_under_adapter(config, task_dir, "native"),
    )


def test_the_authoring_gate_counts_a_world_as_the_replay_world(tmp_path: Path) -> None:
    report = _gate(tmp_path, {"json_db": "initial_state.json", "app_world": WORLD})
    assert report.errors == ()


def test_the_authoring_gate_still_refuses_golden_actions_with_no_world(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="declares no tools.agent.mcp_server"):
        _gate(tmp_path, {"json_db": "initial_state.json"})


def test_the_host_side_engine_refuses_a_world_it_cannot_reach() -> None:
    with pytest.raises(UnbuildableGoldenReplayWorld, match="only the runner replays against"):
        require_golden_replay_world(
            task_dir=Path("."),
            initial_state_json_db="initial_state.json",
            mcp_server=None,
            app_world_url="http://world:8080",
        )


# -- http_request ---------------------------------------------------------------------------


def test_http_request_presents_the_token_to_listed_hosts_alone(
    world_url: str, recorder: HeaderRecorder, recorder_url: str
) -> None:
    tool = HTTPRequestTool(allowed_hosts=[_host(world_url), _host(recorder_url)])
    tool.present_bearer("runtime-token-0123456789", [_host(recorder_url)])

    tool.execute("GET", f"{recorder_url}/anything", headers={"Authorization": "Bearer forged"})
    other = HTTPRequestTool(allowed_hosts=[_host(recorder_url)])
    other.execute("GET", f"{recorder_url}/anything", headers={"Authorization": "Bearer forged"})

    assert recorder.seen == ["Bearer runtime-token-0123456789", None]
    assert "runtime-token" not in json.dumps(tool.get_schema())


def test_http_request_refuses_a_credential_it_could_never_present() -> None:
    tool = HTTPRequestTool(allowed_hosts=[HOST])
    with pytest.raises(ValueError, match="not in allowed_hosts"):
        tool.present_bearer("token-0123456789", ["elsewhere.vendor.example"])
    tool.present_bearer("token-0123456789", [HOST])
    with pytest.raises(ValueError, match="already presented"):
        tool.present_bearer("token-9876543210", [HOST])


@pytest.mark.parametrize(
    ("hosts", "allowed", "unreachable"),
    [
        (["world"], ["world:8080"], []),
        (["world:8080"], ["world"], []),
        (["world:8080"], ["world:8080"], []),
        (["world:8080"], ["world:9090"], ["world:8080"]),
        (["world", "other"], ["world:8080"], ["other"]),
    ],
)
def test_a_world_host_is_reachable_as_http_request_reads_hosts_at_request_time(
    hosts: list[str], allowed: list[str], unreachable: list[str]
) -> None:
    """``host`` names every port, ``host:port`` one port, in the declaration and the tool alike."""
    assert unreachable_hosts(hosts, allowed) == unreachable

    task = _task()
    for actor in ("agent", "user"):
        task["tools"][actor] = {
            "enabled": ["http_request"],
            "http_request": {"allowed_hosts": allowed},
        }
    task["initial_state"]["app_world"] = {**WORLD, "hosts": hosts}
    tool = HTTPRequestTool(allowed_hosts=allowed)
    if unreachable:
        with pytest.raises(ValidationError, match=re.escape(f"{unreachable!r} are not in")):
            TaskConfig.model_validate(task)
        with pytest.raises(ValueError, match=re.escape(f"{unreachable!r} are not in")):
            tool.present_bearer("token-0123456789", hosts)
    else:
        TaskConfig.model_validate(task)
        tool.present_bearer("token-0123456789", hosts)


TOKEN = "runtime-token-0123456789"


def _served_by(
    handler: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str | None]]:
    """Route the tool's requests through ``handler``; the (url, credential) each carried."""
    seen: list[tuple[str, str | None]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return handler(request)

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(recording)) as client:
            return client.request(method, url, **kwargs)

    monkeypatch.setattr("tolokaforge.tools.builtin.http_request.httpx.request", request)
    return seen


def _redirecting_to(target: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == target:
            return httpx.Response(200, json={"arrived": True})
        return httpx.Response(307, headers={"location": target})

    return handler


def test_http_request_keeps_its_bearer_across_a_redirect_within_the_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx drops Authorization on a port change; the world's hop is credentialed anew."""
    seen = _served_by(_redirecting_to("http://world:8080/api/x"), monkeypatch)
    tool = HTTPRequestTool(allowed_hosts=["world"])
    tool.present_bearer(TOKEN, ["world"])

    result = tool.execute("POST", "http://world/api/x", json={"subject": "s"})

    assert result.success, result.error
    assert seen == [
        ("http://world/api/x", f"Bearer {TOKEN}"),
        ("http://world:8080/api/x", f"Bearer {TOKEN}"),
    ]


def test_http_request_drops_its_bearer_when_a_redirect_leaves_the_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _served_by(_redirecting_to("http://elsewhere.example/api/x"), monkeypatch)
    tool = HTTPRequestTool(allowed_hosts=["world", "elsewhere.example"])
    tool.present_bearer(TOKEN, ["world"])

    result = tool.execute("GET", "http://world/api/x")

    assert result.success, result.error
    assert seen == [
        ("http://world/api/x", f"Bearer {TOKEN}"),
        ("http://elsewhere.example/api/x", None),
    ]


def test_http_request_without_a_credential_follows_redirects_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _served_by(_redirecting_to("http://world:8080/api/x"), monkeypatch)

    result = HTTPRequestTool(allowed_hosts=["world"]).execute("GET", "http://world/api/x")

    assert result.success, result.error
    assert [url for url, _ in seen] == ["http://world/api/x", "http://world:8080/api/x"]


def test_a_credentialed_request_refuses_a_redirect_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _served_by(lambda _: httpx.Response(302, headers={"location": "/again"}), monkeypatch)
    tool = HTTPRequestTool(allowed_hosts=["world"])
    tool.present_bearer(TOKEN, ["world"])

    result = tool.execute("GET", "http://world/api/x")

    assert result.success is False
    assert "redirects" in result.error
    assert len(seen) == 6


def test_a_world_answering_401_with_the_credential_echoed_leaks_it_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever surface a refused response reaches, the token is masked there."""

    def echoing_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": f"bad credential {request.headers.get('authorization')}"}
        )

    _served_by(echoing_401, monkeypatch)
    tool = HTTPRequestTool(allowed_hosts=["world"])
    tool.present_bearer(TOKEN, ["world"])

    result = tool.execute("GET", "http://world/api/x")

    assert result.success is False
    assert result.metadata["status_code"] == 401
    assert TOKEN not in result.output
    assert TOKEN not in (result.error or "")
    assert REDACTED_CREDENTIAL in result.output + (result.error or "")


# -- the runner's lifecycle against a world service --------------------------------------


def _http_tool(allowed_hosts: list[str]) -> dict[str, Any]:
    schema = HTTPRequestTool().get_schema()["function"]
    return {
        "name": "http_request",
        "description": schema["description"],
        "parameters": schema["parameters"],
        "tool_config": {"allowed_hosts": allowed_hosts},
    }


def _description(world_url: str, *, extra_hosts: list[str] = ()) -> dict[str, Any]:
    allowed = [_host(world_url), *extra_hosts]
    return {
        "task_id": "app_world_task",
        "name": "App world",
        "category": "test",
        "description": "A help desk world served over HTTP",
        "adapter_type": "native",
        "system_prompt": "You are a help desk agent.",
        "initial_state": {
            "tables": INITIAL_TABLES,
            "app_world": {
                "url": world_url,
                "hosts": [_host(world_url)],
                "actors": {"agent": None, "user": "customer"},
            },
        },
        "agent_tools": [_http_tool(allowed)],
        "user_tools": [_http_tool(allowed)],
        "grading": {
            "combine_method": "weighted",
            "pass_threshold": 1.0,
            "weights": {"state_checks": 1.0},
            "state_checks": {
                "hash_enabled": True,
                "golden_actions": [
                    {
                        "tool_name": "http_request",
                        "arguments": {
                            "method": "POST",
                            "url": f"{world_url}/api/tickets",
                            "json": {"subject": "printer on fire"},
                        },
                    }
                ],
            },
        },
    }


@dataclass
class _Trial:
    """One trial on the in-process runner servicer, driven over its gRPC handlers."""

    runner: Any
    context: Any
    trial_id: str = field(default_factory=lambda: f"world-{uuid.uuid4().hex[:8]}:0")

    def register(self, description: dict[str, Any]) -> pb2.RegisterTrialResponse:
        spec = trial_spec_json(description, trial_id=self.trial_id)
        return self.runner.RegisterTrial(
            register_request(spec, trial_id=self.trial_id), self.context
        )

    def call(self, executor: str, method: str, url: str, **arguments: Any):
        arguments = {"method": method, "url": url, **arguments}
        call_id = f"call_{len(self.recorded)}"
        request = execute_request(
            self.trial_id, "http_request", json.dumps(arguments), executor=executor, call_id=call_id
        )
        return self.runner.ExecuteTool(request, self.context)

    def state(self) -> pb2.GetStateResponse:
        request = pb2.GetStateRequest(trial_id=self.trial_id, include_unstable=True)
        return self.runner.GetState(request, self.context)

    def grade(self) -> pb2.GradeTrialResponse:
        return self.runner.GradeTrial(pb2.GradeTrialRequest(trial_id=self.trial_id), self.context)

    def reset(self) -> pb2.ResetTrialResponse:
        return self.runner.ResetTrial(pb2.ResetTrialRequest(trial_id=self.trial_id), self.context)

    @property
    def recorded(self) -> tuple[Any, ...]:
        return self.runner.trials[self.trial_id].recorded


@pytest.fixture
def trial(runner_service, mock_grpc_context) -> _Trial:
    return _Trial(runner_service, mock_grpc_context)


def test_registration_claims_the_world_loads_its_callers_then_its_tables(
    trial: _Trial, world: FakeAppWorld, world_url: str
) -> None:
    response = trial.register(_description(world_url))

    assert response.success, response.error
    assert world.admin_calls == ["PUT /_admin/tokens", "PUT /_admin/tables"]
    assert world.tables == INITIAL_TABLES
    assert Counter(world.callers.values()) == Counter([None, "customer"])
    minted = {world.admin_token, *world.callers}
    assert len(minted) == 3
    assert minted <= get_default().known_values()


def test_each_actor_reaches_the_world_as_its_own_caller(
    trial: _Trial, world: FakeAppWorld, world_url: str
) -> None:
    trial.register(_description(world_url))

    agent = trial.call("agent", "POST", f"{world_url}/api/tickets", json={"subject": "a"})
    user = trial.call("user", "POST", f"{world_url}/api/tickets", json={"subject": "u"})

    assert agent.status == pb2.EXECUTION_STATUS_SUCCESS, agent.error_message
    assert user.status == pb2.EXECUTION_STATUS_SUCCESS, user.error_message
    assert [t["requester"] for t in world.tables["tickets"]] == ["default", "default", "customer"]


def test_the_world_is_read_back_graded_restored_and_reset(
    trial: _Trial, world: FakeAppWorld, world_url: str
) -> None:
    trial.register(_description(world_url))
    trial.call("agent", "POST", f"{world_url}/api/tickets", json={"subject": "printer on fire"})

    state = trial.state()
    assert state.success, state.error
    subjects = [ticket["subject"] for ticket in json.loads(state.state_json)["tickets"]]
    assert subjects == ["seeded ticket", "printer on fire"]

    graded = trial.grade()
    assert graded.success, graded.error
    assert graded.grade.binary_pass is True

    reset = trial.reset()
    assert reset.success, reset.error
    assert world.tables == INITIAL_TABLES


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ((500, "world exploded"), "refused GET /_admin/tables: HTTP 500 world exploded"),
        ((200, "<html>not json</html>"), "GET /_admin/tables with a body that is not JSON"),
    ],
)
def test_a_world_that_cannot_be_read_back_fails_get_state_loudly(
    trial: _Trial, world: FakeAppWorld, world_url: str, answer: tuple[int, str], message: str
) -> None:
    trial.register(_description(world_url))
    world.read_tables_answer = answer

    state = trial.state()

    assert state.success is False
    assert message in state.error
    service = _host(world_url).split(":")[0]
    assert f"world service {service!r} at {world_url}" in state.error


def _mcp_description(script: str) -> dict[str, Any]:
    """The same world held by a stdio MCP subprocess, as an MCP pack holds it."""
    description = _description("http://unused.example")
    description["agent_tools"] = [
        {
            "name": "create_ticket",
            "description": "File a ticket",
            "parameters": {"type": "object", "properties": {"subject": {"type": "string"}}},
            "source": {
                "toolset": "help_desk",
                "module_path": "mcp_world_server",
                "class_name": "create_ticket",
                "invocation_style": "mcp_server",
                "mcp_server_script": script,
            },
        }
    ]
    description["user_tools"] = []
    description["initial_state"] = {"tables": INITIAL_TABLES}
    description["grading"]["state_checks"]["golden_actions"] = [
        {"tool_name": "create_ticket", "arguments": {"subject": "printer on fire"}}
    ]
    return description


def test_reset_restores_an_mcp_state_holder_as_it_restores_a_world(
    trial: _Trial, tmp_path: Path
) -> None:
    """ResetTrial restores whichever holder keeps the trial's state, not the app world alone."""
    script = tmp_path / "mcp_world_server.py"
    script.write_text(MCP_WORLD_SERVER)
    registered = trial.register(_mcp_description(str(script)))
    assert registered.success, registered.error
    try:
        created = trial.runner.ExecuteTool(
            execute_request(trial.trial_id, "create_ticket", json.dumps({"subject": "s"})),
            trial.context,
        )
        assert created.status == pb2.EXECUTION_STATUS_SUCCESS, created.error_message
        assert len(json.loads(trial.state().state_json)["tickets"]) == 2

        reset = trial.reset()

        assert reset.success, reset.error
        assert json.loads(trial.state().state_json) == INITIAL_TABLES
    finally:
        trial.runner.CleanupTrial(pb2.CleanupTrialRequest(trial_id=trial.trial_id), trial.context)


def test_a_trial_that_diverged_from_the_golden_path_fails_its_hash(
    trial: _Trial, world_url: str
) -> None:
    trial.register(_description(world_url))
    trial.call("agent", "POST", f"{world_url}/api/tickets", json={"subject": "another ticket"})

    graded = trial.grade()

    assert graded.success, graded.error
    assert graded.grade.binary_pass is False


def test_a_trials_credentials_are_forgotten_with_the_trial(
    trial: _Trial, world: FakeAppWorld, world_url: str
) -> None:
    trial.register(_description(world_url))
    minted = {world.admin_token, *world.callers}
    names = [name for name in get_default().list_all_keys() if name.startswith("APP_WORLD_")]
    assert len(names) == 3
    assert minted <= get_default().known_values()

    trial.runner.CleanupTrial(pb2.CleanupTrialRequest(trial_id=trial.trial_id), trial.context)

    assert not [name for name in get_default().list_all_keys() if name.startswith("APP_WORLD_")]
    assert not minted & get_default().known_values()


def test_a_world_claimed_by_someone_else_fails_registration(
    trial: _Trial, world: FakeAppWorld, world_url: str
) -> None:
    world.admin_token = "claimed-by-another-container"

    response = trial.register(_description(world_url))

    assert response.success is False
    assert "another admin token was bound first" in response.error
    assert trial.trial_id not in trial.runner.trials
    assert world.tables is None
    assert not [name for name in get_default().list_all_keys() if name.startswith("APP_WORLD_")]


def test_no_credential_reaches_a_host_outside_the_world(
    trial: _Trial, world_url: str, recorder: HeaderRecorder, recorder_url: str
) -> None:
    trial.register(_description(world_url, extra_hosts=[_host(recorder_url)]))

    for executor in ("agent", "user"):
        forged = {"Authorization": "Bearer mine"}
        trial.call(executor, "GET", f"{recorder_url}/anything", headers=forged)

    assert recorder.seen == [None, None]


def test_no_token_reaches_the_model_the_trajectory_or_the_logs(
    trial: _Trial, world: FakeAppWorld, world_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    install_global_redactor()
    caplog.set_level(logging.DEBUG)

    registered = trial.register(_description(world_url))
    echoed = trial.call("agent", "GET", f"{world_url}/api/debug/echo")
    created = trial.call("user", "POST", f"{world_url}/api/tickets", json={"subject": "s"})
    trial.state()
    trial.grade()
    tokens = [world.admin_token, *world.callers]
    logging.getLogger("tolokaforge.runner.service").info("leaked %s", tokens[1])

    model_side = json.dumps(
        {
            "schemas": [schema.parameters_json for schema in registered.tool_schemas],
            "outputs": [echoed.output, echoed.error_message, created.output],
            "trajectory": [call.model_dump(mode="json") for call in trial.recorded],
        }
    )
    assert REDACTED_CREDENTIAL in echoed.output
    assert f"leaked {PLACEHOLDER}" in caplog.text
    for token in tokens:
        assert token not in model_side
        assert token not in caplog.text
