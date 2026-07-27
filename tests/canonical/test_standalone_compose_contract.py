"""Locks the wiring contract of the standalone four-service compose recipe.

``deploy/standalone/docker-compose.yaml`` is a published reference recipe (a
compatibility surface): a cold user runs the four ``tolokasoft1/tolokaforge-*``
images with ``docker compose up`` and expects the same wiring the in-tree stack
uses. This guard parses the file and asserts that wiring — image references, the
tag variable, service DNS names, the runner's env, the ``json-db`` alias, the
``service_healthy`` startup ordering — so any unintended field change trips CI.
It also asserts the absence cases that carry contract meaning: no ``MOCK_WEB_URL``
(the runner reads no such var), no host port beyond the runner's ``50051``, and
no re-declared ``healthcheck`` (each image self-reports its own). A parsed
structure golden, not a byte golden: it locks the contract, not incidental YAML
or comment formatting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "standalone" / "docker-compose.yaml"
_ENV_EXAMPLE = _REPO_ROOT / "deploy" / "standalone" / ".env.example"

_IMAGE_TAG_REF = "${TOLOKAFORGE_IMAGE_TAG:-latest}"
_EXPECTED_SERVICES = {"runner", "db-service", "rag-service", "mock-web"}


def _load_compose() -> dict:
    assert _COMPOSE_FILE.exists(), (
        f"{_COMPOSE_FILE.relative_to(_REPO_ROOT)} is missing — the standalone "
        "compose recipe must ship"
    )
    parsed = yaml.safe_load(_COMPOSE_FILE.read_text())
    assert isinstance(parsed, dict) and parsed.get("services"), (
        "standalone docker-compose.yaml parsed to an empty document — "
        "expected a populated compose file with a `services` mapping"
    )
    return parsed


def test_service_names_and_image_refs() -> None:
    compose = _load_compose()
    services = compose["services"]
    assert set(services) == _EXPECTED_SERVICES, (
        "the standalone stack is exactly the four first-party services; the "
        "compose service names double as the DNS names the runner env and task "
        "code resolve"
    )
    for component in _EXPECTED_SERVICES:
        expected = f"tolokasoft1/tolokaforge-{component}:{_IMAGE_TAG_REF}"
        assert services[component]["image"] == expected, (
            f"{component} must reference the published image via the "
            f"TOLOKAFORGE_IMAGE_TAG variable ({expected})"
        )


def test_runner_env_and_port() -> None:
    runner = _load_compose()["services"]["runner"]
    assert runner["ports"] == [
        "50051:50051"
    ], "only the runner's gRPC port is published to the host"
    env = runner["environment"]
    assert env["DB_SERVICE_URL"] == "http://db-service:8000"
    assert env["RAG_SERVICE_URL"] == "http://rag-service:8001"
    assert "MOCK_WEB_URL" not in env, (
        "the runner reads no MOCK_WEB_URL — mock-web is reached by service-name "
        "DNS, so injecting the var would be dead wiring"
    )


def test_only_runner_publishes_a_host_port() -> None:
    services = _load_compose()["services"]
    published = {name for name, spec in services.items() if "ports" in spec}
    assert published == {"runner"}, (
        "the three peers stay internal to the compose network; only the runner "
        f"publishes a host port (found published: {sorted(published)})"
    )


def test_mock_web_and_rag_wiring() -> None:
    services = _load_compose()["services"]
    mock_web = services["mock-web"]
    assert mock_web["environment"]["JSON_DB_URL"] == "http://db-service:8000"
    assert mock_web["environment"]["PYTHONUNBUFFERED"] == "1", (
        "mock-web's image bakes no PYTHONUNBUFFERED; the recipe sets it so its "
        "logs stream unbuffered like the other three services'"
    )
    rag = services["rag-service"]
    assert rag["environment"]["CORPUS_PATH"] == "/env/rag/corpus"
    assert "rag_data:/env/rag" in rag["volumes"], (
        "rag-service persists its corpus/index in the rag_data named volume, "
        "mirroring the in-tree full stack"
    )


def test_db_service_json_db_alias() -> None:
    db = _load_compose()["services"]["db-service"]
    aliases = db["networks"]["runner-net"]["aliases"]
    assert "json-db" in aliases, (
        "db-service carries the json-db network alias so http://json-db:8000 "
        "resolves exactly as it does on the in-tree stack"
    )


def test_startup_ordering_on_service_healthy() -> None:
    services = _load_compose()["services"]
    for dependent in ("runner", "mock-web"):
        dep = services[dependent]["depends_on"]["db-service"]
        assert (
            dep["condition"] == "service_healthy"
        ), f"{dependent} must wait for db-service to report healthy before it starts"
    assert "depends_on" not in services["db-service"]
    assert "depends_on" not in services["rag-service"], (
        "rag-service has no dependents and no dependencies; the runner is not "
        "gated on its cold start"
    )


def test_no_service_redeclares_a_healthcheck() -> None:
    services = _load_compose()["services"]
    redeclared = {name for name, spec in services.items() if "healthcheck" in spec}
    assert not redeclared, (
        "each image self-reports its own HEALTHCHECK; re-declaring one in the "
        f"recipe would fork the source of truth (found: {sorted(redeclared)})"
    )


def test_network_and_volume_declared() -> None:
    compose = _load_compose()
    assert (
        "runner-net" in compose["networks"]
    ), "the single compose network is named explicitly so the file is self-documenting"
    assert "rag_data" in compose["volumes"]


def test_env_example_documents_key_surfaces() -> None:
    assert _ENV_EXAMPLE.exists(), (
        f"{_ENV_EXAMPLE.relative_to(_REPO_ROOT)} is missing — the recipe ships a "
        "credential template"
    )
    text = _ENV_EXAMPLE.read_text()
    for token in ("TOLOKAFORGE_IMAGE_TAG", "OPENROUTER_API_KEY", "TOLOKAFORGE_SECRETS_JSON"):
        assert token in text, f".env.example must document {token}"
