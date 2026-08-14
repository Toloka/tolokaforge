"""The bundle a trial whose environment never came up leaves on disk.

Written by the production path — ``ProvisioningTrialExecutor.execute`` reaching
its ``ProvisionError`` branch, which synthesises the result and writes the
minimal bundle — rather than by hand, so a test reading this shape reads what a
run actually writes: ``trajectory.yaml`` and ``metrics.yaml``, a
``termination_reason`` of ``provision_error``, and **no** ``task.yaml``, because
the conductor never ran and nothing else writes that trial's directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from tests.canonical._factories import make_task_config, make_trial_spec
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.output.artifacts import FileArtifactWriter, TrialArtifactWriter
from tolokaforge.core.runtime import EnvHandle, InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial import TrialSpec
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

PROVISION_FAILURE_FILES = ("metrics.yaml", "trajectory.yaml")
"""Every file the writer produces for such a trial — the assertion a caller of
:func:`write_provision_failure_bundle` makes when the *absence* of the rest is
its subject."""


class FailProvisionBackend(InMemoryRuntimeBackend):
    """A backend whose ``provision`` always raises, with a caller-chosen reason.

    The direct route into :meth:`ProvisioningTrialExecutor.execute`'s failure
    branch, where :func:`write_provision_failure_bundle` reaches it the long way
    round through a readiness timeout.
    """

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    def provision(self, spec: TrialSpec) -> EnvHandle:
        raise ProvisionError(trial_id=spec.trial_id, stage="provision", reason=self._reason)


def provisioning_executor(
    backend: InMemoryRuntimeBackend,
    output_dir: Path,
    artifact_writer: TrialArtifactWriter,
    *,
    logger: StructuredLogger,
) -> ProvisioningTrialExecutor:
    """The executor under test, with the conductor that is never reached."""
    return ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=InMemoryConductor(),
        logger=logger,
        output_dir=output_dir,
        artifact_writer=artifact_writer,
    )


def write_provision_failure_bundle(
    output_dir: Path, *, task_id: str = "refund_task", trial_index: int = 0
) -> Path:
    """Drive one trial into provisioning failure under ``output_dir``; return its bundle.

    The backend times out awaiting readiness, which is the branch that reaches
    the writer with a real ``ProvisionError``. The injected logger is asserted
    silent on warnings because the writer swallows its own I/O failures — without
    that check a fixture that wrote nothing would arrive at the test as a bundle.
    """
    logger = MagicMock()
    executor = provisioning_executor(
        InMemoryRuntimeBackend(await_ready_times_out=True),
        output_dir,
        FileArtifactWriter(),
        logger=logger,
    )
    spec = make_trial_spec(trial_id=f"{task_id}:{trial_index}", task_id=task_id)

    executor.execute(spec, make_task_config())

    assert logger.warning.call_args_list == [], logger.warning.call_args_list
    bundle = output_dir / "trials" / task_id / str(trial_index)
    written = sorted(path.name for path in bundle.iterdir())
    assert written == sorted(PROVISION_FAILURE_FILES), written
    return bundle
