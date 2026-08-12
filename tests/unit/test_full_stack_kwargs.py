"""What the stack factories put on the runner container, and for which services.

``full_stack`` must accept and forward every kwarg ``core_stack`` does: the
orchestrator passes the same kwargs (including ``enable_playwright``,
``task_pack_mounts``, ``extra_runner_binds``, ``mount_docker_socket``,
``enable_dind``) regardless of which stack factory it picks. Without this,
mobile/browser tasks (which trigger the full_stack switch) would lose
Playwright + bind mounts and fail at first action.

The service-address variables are honest absences: ``RAG_SERVICE_URL`` and
``TYPESENSE_HOST`` / ``TYPESENSE_PORT`` are present iff the run actually has
that plane, so "variable present" carries information. The TypeSense API key
is not among them — it reaches the runner only inside
``TOLOKAFORGE_SECRETS_JSON``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.compose_materialisation import inject_runner_credentials
from tolokaforge.docker.stacks.core import TypeSenseAddress, core_stack
from tolokaforge.docker.stacks.full import full_stack
from tolokaforge.secrets import CONTAINER_SECRETS_ENV_VAR, register_runtime_secret

pytestmark = pytest.mark.unit

TYPESENSE_ADDRESS = TypeSenseAddress(host="typesense", port=8108)
GENERATED_API_KEY = "typesense-generated-key-0123456789"


def _runner_def(stack):
    runner = stack.services.get("runner")
    assert runner is not None, "runner ServiceDefinition not found in stack"
    return runner


def test_core_stack_runner_omits_rag_service_url():
    """The core stack provisions no rag-service, so the runner container must
    NOT carry ``RAG_SERVICE_URL``. An honest absence is load-bearing: the
    runner only builds a RAG client (and the judge only gets a ``search_kb``
    tool) when this var is present, so a stray value here would point the
    judge at a host that does not exist on this stack — it would then grade
    silently without reading the KB it grades against (issue #95)."""
    runner = _runner_def(core_stack())
    assert "RAG_SERVICE_URL" not in runner.environment


def test_full_stack_runner_injects_rag_service_url():
    """The full stack DOES run a rag-service, so the runner container carries
    ``RAG_SERVICE_URL`` pointing at it. This is what makes the agent's
    full-stack RAG client non-None and the judge's search_kb reachable."""
    runner = _runner_def(full_stack())
    assert runner.environment.get("RAG_SERVICE_URL") == "http://tolokaforge-rag-service:8001"


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_a_stack_with_no_typesense_plane_carries_no_address(factory):
    """No plane, no variables — on any service.

    The runner reads "``TYPESENSE_HOST`` is set" as "this run has a TypeSense
    plane, use it instead of whatever address my task description carries". A
    value left behind by a run that configured no plane would point every
    knowledge-base trial at a host nothing answers on.
    """
    for service in factory().services.values():
        assert "TYPESENSE_HOST" not in service.environment
        assert "TYPESENSE_PORT" not in service.environment


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_only_the_runner_is_told_where_typesense_is(factory):
    """The address goes on the runner service definition and on no other.

    Only the runner registers TypeSense clients. db-service, rag-service and
    mock-web have no business holding the address, and a stray copy would
    outlive the runner's own resolution rules.
    """
    stack = factory(typesense_address=TYPESENSE_ADDRESS)

    carriers = sorted(
        name for name, service in stack.services.items() if "TYPESENSE_HOST" in service.environment
    )
    assert carriers == ["runner"]

    runner = _runner_def(stack)
    assert runner.environment["TYPESENSE_HOST"] == "typesense"
    assert runner.environment["TYPESENSE_PORT"] == "8108"


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_the_typesense_api_key_is_never_an_environment_literal(
    factory, isolated_secret_manager
) -> None:
    """The key crosses the host→container boundary only inside the secrets payload.

    Registering it with the SecretManager before the stack is built is what
    puts it in ``TOLOKAFORGE_SECRETS_JSON`` — and, in the same move, in the
    log-redaction set. Handing it to a container as its own environment entry
    would transport it just as well and redact nothing, so this sweep runs
    over *every* service definition rather than over the runner alone.

    The unconditional subscript at the end doubles as the regression lock that
    the engine-built stack injects the payload at all: drop the injection and
    this test raises ``KeyError`` rather than passing vacuously.
    """
    register_runtime_secret("TYPESENSE_API_KEY", GENERATED_API_KEY)

    stack = factory(typesense_address=TYPESENSE_ADDRESS)

    for name, service in stack.services.items():
        for var, value in service.environment.items():
            assert var != "TYPESENSE_API_KEY", f"{name} carries the key as its own variable"
            assert value != GENERATED_API_KEY, f"{name}.{var} carries the key verbatim"

    payload = json.loads(_runner_def(stack).environment["TOLOKAFORGE_SECRETS_JSON"])
    assert payload["TYPESENSE_API_KEY"] == GENERATED_API_KEY


def test_both_injection_sites_deliver_the_same_payload(
    tmp_path: Path, installed_fake_secrets
) -> None:
    """One payload definition, two injection sites, one escaping rule.

    The compose transform doubles ``$`` because Docker Compose interpolates it
    in ``environment`` values; the core stack hands its value to the Docker
    SDK, which interpolates nothing. De-escaping the compose-path value must
    reproduce the core stack's byte for byte — that fails if either site's
    payload diverges, if the escape moves to the shared helper (the core stack
    would start delivering doubled dollars), or if it is applied twice.
    """
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text("services:\n  runner:\n    image: r:local\n")
    inject_runner_credentials(compose_file, "runner")

    written = yaml.safe_load(compose_file.read_text())["services"]["runner"]["environment"][
        CONTAINER_SECRETS_ENV_VAR
    ]
    core_value = _runner_def(core_stack()).environment[CONTAINER_SECRETS_ENV_VAR]

    assert "$" in core_value, "the fake payload lost its $-bearing value; this test stops biting"
    assert "$$" in written
    assert written.replace("$$", "$") == core_value


def test_full_stack_includes_db_runner_and_extras():
    stack = full_stack()
    services = set(stack.services.keys())
    assert {"db-service", "runner", "rag-service", "mock-web"}.issubset(services)


def test_full_stack_forwards_enable_playwright():
    stack = full_stack(enable_playwright=True)
    runner = _runner_def(stack)
    assert runner.build_args.get("INSTALL_PLAYWRIGHT") == "true"


def test_full_stack_forwards_extra_runner_binds(tmp_path: Path):
    bind_src = tmp_path / "logs"
    bind_src.mkdir()
    stack = full_stack(extra_runner_binds=[(bind_src, "/var/log/runner")])
    runner = _runner_def(stack)
    bind_targets = [m.target for m in runner.mounts]
    assert "/var/log/runner" in bind_targets


def test_full_stack_forwards_task_pack_mounts(tmp_path: Path):
    pack = tmp_path / "tasks"
    pack.mkdir()
    stack = full_stack(task_pack_mounts=[pack])
    runner = _runner_def(stack)
    abs_pack = str(pack.resolve())
    bind_targets = [m.target for m in runner.mounts]
    assert abs_pack in bind_targets


def test_full_stack_forwards_mount_docker_socket():
    stack = full_stack(mount_docker_socket=True)
    runner = _runner_def(stack)
    bind_targets = [m.target for m in runner.mounts]
    assert "/var/run/docker.sock" in bind_targets


def test_full_stack_default_omits_playwright():
    stack = full_stack()
    runner = _runner_def(stack)
    assert "INSTALL_PLAYWRIGHT" not in runner.build_args


def test_full_stack_forwards_enable_docker_cli():
    stack = full_stack(enable_docker_cli=True)
    runner = _runner_def(stack)
    assert runner.build_args.get("INSTALL_DOCKER_CLI") == "true"


def test_full_stack_default_omits_docker_cli():
    stack = full_stack()
    runner = _runner_def(stack)
    assert "INSTALL_DOCKER_CLI" not in runner.build_args


def test_full_stack_mock_web_pins_build_context():
    """``mock-web`` declares ``context_files`` so its image-content-hash
    only depends on the service's own source. Without this the orchestrator
    hashes the whole repo and the cache fires on every unrelated edit
    (observed during PR #127 verification — repeated 10+ minute rebuilds
    triggered by changes that mock-web's Dockerfile never COPYs in).
    """
    stack = full_stack()
    mock_web = stack.services.get("mock-web")
    assert mock_web is not None
    assert mock_web.context_files == ["tolokaforge/env/mock_web_service/"]


def test_full_stack_rag_service_pins_build_context():
    """``rag-service`` context_files contains the resolved wheel (for
    ``import tolokaforge.secrets``) and the service's own directory."""
    stack = full_stack()
    rag_service = stack.services.get("rag-service")
    assert rag_service is not None
    assert len(rag_service.context_files) == 2
    assert rag_service.context_files[0].endswith(".whl")
    assert rag_service.context_files[1] == "tolokaforge/env/rag_service/"
