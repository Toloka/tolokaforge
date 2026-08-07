"""A TypeSense server that never became ready must abort the run, not the log line.

``TypeSenseServerManager.start()`` reports failure by returning ``False`` — the
Docker foundation layer is unimportable, or the container never answered its
readiness probes and was stopped again. An orchestrator that discards that
boolean writes the *would-be* address into ``config.orchestrator.typesense`` and
announces "TypeSense server started" for a server that is not running; inside the
runner container that host-side address is the runner itself, so every
``search_policy`` call fails and its trials still grade as agent behaviour (#925,
#926).

The orchestrator is real here; only the server-manager factory — the Docker
boundary — is a stand-in, the same seam
``test_orchestrator_typesense_cache_invalidation.py`` substitutes at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tolokaforge.core.search.typesense_server as typesense_server_module
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TypeSenseConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = pytest.mark.unit

CONFIGURED_TIMEOUT = 17.5


class _Manager:
    """The started-server handle, answering ``start()`` with a fixed verdict."""

    def __init__(self, *, started: bool) -> None:
        self.host = "127.0.0.1"
        self.port = 8199
        self.api_key = "resolved-api-key"
        self._started = started

    def start(self) -> bool:
        return self._started


def _orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                auto_start_services=False,
                typesense=TypeSenseConfig(
                    enabled=True,
                    mode="local",
                    port="auto",
                    api_key=None,
                    timeout=CONFIGURED_TIMEOUT,
                ),
            ),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "results"), projects=[str(tmp_path)]
            ),
        ),
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )


def _with_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, started: bool
) -> Orchestrator:
    monkeypatch.setattr(
        typesense_server_module,
        "create_typesense_server",
        lambda **_kwargs: _Manager(started=started),
    )
    orchestrator = _orchestrator(tmp_path)
    orchestrator._typesense_server = None
    return orchestrator


def test_a_server_that_never_became_ready_aborts_before_the_config_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed start must not leave a started-server address behind in the run config."""
    orchestrator = _with_manager(tmp_path, monkeypatch, started=False)

    with pytest.raises(RuntimeError):
        orchestrator._ensure_typesense_started()

    assert orchestrator.config.orchestrator.typesense.port == "auto"
    assert orchestrator.config.orchestrator.typesense.api_key is None


def test_the_abort_names_the_component_the_address_tried_and_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator must read the failure once, not wrapped in the start handler's prefix."""
    orchestrator = _with_manager(tmp_path, monkeypatch, started=False)

    with pytest.raises(RuntimeError) as raised:
        orchestrator._ensure_typesense_started()

    message = str(raised.value)
    # The raise sits outside the try whose handler re-wraps everything it catches.
    assert "Failed to start TypeSense server" not in message
    # Leads with the component: "orchestrator.typesense" also appears in the remedy,
    # so containment alone would pass on a message that never names the component.
    assert message.startswith("orchestrator.typesense:")
    assert "127.0.0.1:8199" in message
    assert str(CONFIGURED_TIMEOUT) in message


def test_a_started_server_resolves_its_port_and_api_key_into_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path is unchanged: the adapter still reads the resolved address."""
    orchestrator = _with_manager(tmp_path, monkeypatch, started=True)

    orchestrator._ensure_typesense_started()

    assert orchestrator.config.orchestrator.typesense.port == 8199
    assert orchestrator.config.orchestrator.typesense.api_key == "resolved-api-key"
