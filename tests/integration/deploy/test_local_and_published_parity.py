"""Local-vs-published debugging-runbook parity for the runner image.

Umbrella #610 constraint 1: one debugging runbook, two sources. The same fixed
operator runbook (``tolokaforge --version``, a gRPC channel-ready health probe,
and a log-tail — see ``run_debugging_runbook``) must produce the same observable
outcome whether the runner image was built locally via ``make docker-build`` or
pulled as the published ``tolokasoft1/tolokaforge-runner:latest``. A divergence
in the runbook outcome fails the parity assertion.

The two sources:

- ``local`` — the runner image the current tree produces (``make docker-build``
  tags it by content hash; resolved via ``expected_image_ref``). The
  developer-flow side: it runs in any integration run with Docker up and the
  runner image built.
- ``published`` — pulled ``tolokasoft1/tolokaforge-runner:latest``. Absent until
  the first stable publish, so it is skip-guarded on image availability; it runs
  in the nightly/release lane once the tag exists.

The runbook outcome is source-independent by construction: the volatile version
string is reduced to a well-formedness flag, so a locally-built dev image and a
published release whose version numbers legitimately differ still compare equal
when they behave identically. What diverges — and fails — is a source whose CLI
version command is broken or whose gRPC server never reaches serving.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.integration.deploy.conftest import (
    RunbookOutcome,
    published_image_ref,
    run_debugging_runbook,
    run_standalone,
)
from tests.utils.docker_helpers import current_image_id
from tolokaforge.docker.builder import expected_image_ref

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.slow,
]

_HEALTHY_RUNBOOK = RunbookOutcome(version_ok=True, grpc_serving=True, log_tail_nonempty=True)

_UNAVAILABLE_REASON = {
    "local": "local runner image not built — run make docker-build",
    "published": "tolokasoft1/tolokaforge-runner:latest not available until the first publish",
}


def _resolve_ref(source: str) -> str | None:
    """The runner image reference for ``source``, or ``None`` when unavailable."""
    if source == "local":
        return expected_image_ref("runner") if current_image_id("runner") else None
    ref = published_image_ref("runner", "latest")
    pulled = subprocess.run(["docker", "pull", ref], capture_output=True, text=True)
    return ref if pulled.returncode == 0 else None


@pytest.fixture(scope="module")
def runbook_outcomes(docker_daemon: None) -> dict[str, RunbookOutcome]:
    """Runbook outcome per available source, keyed ``local`` / ``published``.

    Each available source's runner image is started standalone, driven through
    the debugging runbook, and torn down; unavailable sources are omitted so the
    consuming tests skip them explicitly.
    """
    outcomes: dict[str, RunbookOutcome] = {}
    for source in ("local", "published"):
        ref = _resolve_ref(source)
        if ref is None:
            continue
        container_id = run_standalone(ref)
        try:
            outcomes[source] = run_debugging_runbook(container_id)
        finally:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
    return outcomes


@pytest.mark.parametrize("source", ["local", "published"])
def test_runbook_outcome_per_source(
    runbook_outcomes: dict[str, RunbookOutcome], source: str
) -> None:
    """The debugging runbook produces the healthy outcome against each source."""
    outcome = runbook_outcomes.get(source)
    if outcome is None:
        pytest.skip(_UNAVAILABLE_REASON[source])
    assert outcome == _HEALTHY_RUNBOOK, f"{source} runbook diverged: {outcome}"


def test_local_and_published_runbook_parity(
    runbook_outcomes: dict[str, RunbookOutcome],
) -> None:
    """The runbook yields the identical observable outcome for both sources."""
    if "published" not in runbook_outcomes:
        pytest.skip(_UNAVAILABLE_REASON["published"])
    if "local" not in runbook_outcomes:
        pytest.skip(_UNAVAILABLE_REASON["local"])
    local, published = runbook_outcomes["local"], runbook_outcomes["published"]
    assert (
        local == published
    ), f"debugging runbook diverged across sources: local={local} published={published}"
