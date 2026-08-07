"""A TypeSense server that never became ready must abort the run, not the log line.

``TypeSenseServerManager.start()`` reports failure by returning ``False`` — the
Docker foundation layer is unimportable, or the container never answered its
readiness probes and was stopped again. An orchestrator that discards that
boolean writes the *would-be* address into ``config.orchestrator.typesense`` and
announces "TypeSense server started" for a server that is not running; inside the
runner container that host-side address is the runner itself, so every
``search_policy`` call fails and its trials still grade as agent behaviour (#925,
#926).

The orchestrator is real here; only ``create_typesense_server`` — the factory
the method imports the Docker boundary through — is a stand-in.
``test_orchestrator_typesense_bridge.py`` covers the next step of the same
chain and substitutes further down, at the ``docker`` module and an
already-assigned server handle.
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
from tolokaforge.secrets import get_default

pytestmark = pytest.mark.unit

CONFIGURED_TIMEOUT = 17.5


class _Manager:
    """The started-server handle, answering ``start()`` with a fixed verdict.

    ``port`` defaults to the sentinel the real manager carries until ``start()``
    resolves one, so a row that wants a resolved address must say so.
    """

    def __init__(self, *, started: bool, port: int = -1) -> None:
        self.host = "127.0.0.1"
        self.port = port
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, started: bool, port: int = 8199
) -> Orchestrator:
    monkeypatch.setattr(
        typesense_server_module,
        "create_typesense_server",
        lambda **_kwargs: _Manager(started=started, port=port),
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


@pytest.mark.parametrize(
    ("row", "port", "present", "absent"),
    [
        ("port-resolved", 8199, "127.0.0.1:8199", "no port was ever resolved"),
        # The manager resolves its port inside ``start()``, so the path that
        # fails first — an unimportable Docker foundation layer — still carries
        # the ``-1`` sentinel here.
        ("port-never-resolved", -1, "no port was ever resolved", "127.0.0.1:-1"),
    ],
)
def test_the_abort_names_the_component_what_was_tried_and_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str, port: int, present: str, absent: str
) -> None:
    """The operator must read the failure once, not wrapped in the start handler's prefix."""
    orchestrator = _with_manager(tmp_path, monkeypatch, started=False, port=port)

    with pytest.raises(RuntimeError) as raised:
        orchestrator._ensure_typesense_started()

    message = str(raised.value)
    # The raise sits outside the try whose handler re-wraps everything it catches.
    assert "Failed to start TypeSense server" not in message
    # Leads with the component: "orchestrator.typesense" also appears in the remedy,
    # so containment alone would pass on a message that never names the component.
    assert message.startswith("orchestrator.typesense:")
    assert present in message
    assert absent not in message
    assert "127.0.0.1" in message
    assert str(CONFIGURED_TIMEOUT) in message


def test_a_started_server_resolves_its_port_and_api_key_into_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path is unchanged: the adapter still reads the resolved address."""
    orchestrator = _with_manager(tmp_path, monkeypatch, started=True)

    orchestrator._ensure_typesense_started()

    assert orchestrator.config.orchestrator.typesense.port == 8199
    assert orchestrator.config.orchestrator.typesense.api_key == "resolved-api-key"


def test_the_resolved_api_key_enters_the_secret_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_secret_manager
) -> None:
    """A key this process generated is a secret like any other from here on.

    Registration is what carries it to the runner — ``core_stack`` serialises
    the manager into ``TOLOKAFORGE_SECRETS_JSON`` and nothing else transports
    it — and it is what puts the value in the redaction set before the
    "Creating adapter" params line renders it.
    """
    orchestrator = _with_manager(tmp_path, monkeypatch, started=True)

    orchestrator._ensure_typesense_started()

    manager = get_default()
    assert manager.get_secret("TYPESENSE_API_KEY") == "resolved-api-key"
    assert manager.serialize()["TYPESENSE_API_KEY"] == "resolved-api-key"


def test_a_plane_with_no_key_of_its_own_registers_nothing(
    tmp_path: Path, isolated_secret_manager
) -> None:
    """``mode: remote`` without a key leaves the installed manager untouched.

    Registration replaces the singleton, so an unchanged manager is the
    observable "nothing was registered". Registering an absent credential
    would raise on the empty value, and registering a placeholder would
    shadow a configured ``TYPESENSE_API_KEY`` with nothing.
    """
    orchestrator = _orchestrator(tmp_path)
    orchestrator.config.orchestrator.typesense = TypeSenseConfig(
        enabled=True, mode="remote", host="ts.example", port=8108, api_key=None
    )
    installed = get_default()

    orchestrator._ensure_typesense_started()

    assert get_default() is installed
