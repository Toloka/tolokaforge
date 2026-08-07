"""The address the runner container is created with, and where it comes from.

At stack-build time the run config still holds the *host-side* TypeSense
address — the one the orchestrator itself indexes against. Inside the runner
container that address is the runner itself (#925), so a bridged server is
never described by it: the runner is told the network alias and the container
port, which are static by construction.

The same enablement question decides whether the adapter is handed connection
details and whether the runner container is told an address. They are one
predicate, so a run can never end up with a plane for one and not the other.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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

HOST_SIDE_HOST = "127.0.0.1"
HOST_SIDE_PORT = 61234


def _orchestrator(tmp_path: Path, typesense: TypeSenseConfig) -> Orchestrator:
    return Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                auto_start_services=False,
                typesense=typesense,
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


def test_a_bridged_server_is_injected_as_its_alias_not_as_the_configured_address(
    tmp_path: Path,
) -> None:
    """The alias wins while the run config still says ``127.0.0.1:61234``.

    That second assertion is the point: reading ``config.orchestrator.typesense``
    here would produce the host-side address, which is what the deleted
    post-start rewrite existed to repair.
    """
    orchestrator = _orchestrator(
        tmp_path,
        TypeSenseConfig(
            enabled=True,
            mode="local",
            host=HOST_SIDE_HOST,
            port=HOST_SIDE_PORT,
            api_key="resolved-api-key",
        ),
    )
    orchestrator._typesense_server = SimpleNamespace(
        host=HOST_SIDE_HOST, port=HOST_SIDE_PORT, api_key="resolved-api-key"
    )

    kwargs = orchestrator._typesense_stack_kwargs()

    address = kwargs["typesense_address"]
    assert (address.host, address.port) == ("typesense", 8108)
    configured = orchestrator.config.orchestrator.typesense
    assert (configured.host, configured.port) == (HOST_SIDE_HOST, HOST_SIDE_PORT)


def test_a_plane_this_process_did_not_start_is_injected_verbatim(tmp_path: Path) -> None:
    """An external server is reachable at the address the run config names."""
    orchestrator = _orchestrator(
        tmp_path,
        TypeSenseConfig(enabled=True, mode="remote", host="ts.example", port=8108),
    )

    address = orchestrator._typesense_stack_kwargs()["typesense_address"]

    assert (address.host, address.port) == ("ts.example", 8108)


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_an_unresolved_port_refuses_to_build_the_stack(tmp_path: Path, mode: str) -> None:
    """``auto`` is not an address, and it must not reach a container as one.

    Reached when ``run()`` is handed pre-loaded tasks: it then skips
    ``load_tasks()``, so ``_ensure_typesense_started`` never resolves a port
    and no server handle exists to derive an alias from.
    """
    orchestrator = _orchestrator(
        tmp_path, TypeSenseConfig(enabled=True, mode=mode, host="ts.example", port="auto")
    )

    with pytest.raises(RuntimeError) as raised:
        orchestrator._typesense_stack_kwargs()

    message = str(raised.value)
    assert message.startswith("orchestrator.typesense:")
    assert "auto" in message
    assert mode in message


@pytest.mark.parametrize(
    ("row", "enabled", "mode", "has_plane"),
    [
        ("enabled-local", True, "local", True),
        ("enabled-remote", True, "remote", True),
        ("enabled-disabled-mode", True, "disabled", False),
        ("off-local", False, "local", False),
        ("off-remote", False, "remote", False),
        ("off-disabled-mode", False, "disabled", False),
    ],
)
@patch("tolokaforge.core.orchestrator.get_adapter")
def test_the_stack_address_and_the_adapter_answer_the_same_enablement_question(
    mock_get_adapter: MagicMock, tmp_path: Path, row: str, enabled: bool, mode: str, has_plane: bool
) -> None:
    """One predicate decides both, so the two can never disagree.

    A second spelling of "is the plane on" would let a run hand the adapter
    connection details while creating a runner that was told no address, or
    the reverse — the split-brain this stage exists to close.
    """
    mock_get_adapter.return_value = MagicMock()
    orchestrator = _orchestrator(
        tmp_path,
        TypeSenseConfig(enabled=enabled, mode=mode, host="ts.example", port=8108),
    )

    orchestrator._create_adapter()
    kwargs = orchestrator._typesense_stack_kwargs()

    assert ("typesense" in mock_get_adapter.call_args[0][1]) is has_plane
    assert ("typesense_address" in kwargs) is has_plane
