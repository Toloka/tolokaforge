"""Agent driver Strategy — "how the agent produces turns during a trial".

Adapters describe *what the task is* — the pack directory, the container
stack it runs in, the grading declaration, the tool schemas the runner
should install. Drivers describe *how the agent runs against it* — an
engine-side LLM turn loop, a vendor coding-harness CLI in-container, or
(future) a hybrid multi-model orchestrator.

The two concerns live in separate objects so that:

- Adapter fixes cannot regress harness mode and vice versa.
- Adding a new mode is one new :class:`AgentDriver` class; no adapter
  needs a mode branch.
- Adding a new coding-harness vendor is one new :class:`HarnessSpec` in
  the registry — the driver handles all vendors.

The orchestrator selects one driver per run (see
:meth:`~tolokaforge.core.orchestrator.Orchestrator._select_driver`) and
applies it between the adapter's task description and the wire message.
The engine-side dispatch seam is the free-form
``TaskDescription.metadata`` dict — the driver is responsible for
populating whatever keys the runner / conductor look for
(``agent_harness_command`` today).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.adapters.base import ComposeImageBuild
    from tolokaforge.runner.models import TaskDescription


@dataclass(frozen=True)
class StagedTask:
    """One task's per-trial staging root as materialised by the adapter.

    Adapters expose :meth:`~tolokaforge.adapters.base.BaseAdapter.stage_task`
    to hand a driver the small set of artefacts it needs to layer harness
    installs, provider-env injections, or sidecar services on top of the
    pack's own compose stack — without the adapter itself knowing whether
    a driver will apply layers or not.

    Attributes:
        task_id: Task identifier — the driver mixes it into per-task
            image tags so several tasks can build in parallel without
            trampling each other's cached images.
        staging_dir: Absolute path to the per-task staging directory. Any
            files a driver writes (a harness Dockerfile snippet, an
            install script) go under this dir; the compose file's
            ``build.context: .`` resolves against it.
        compose_file: Absolute path to the compose file inside
            ``staging_dir``. The driver may mutate its content in place
            (via :class:`StagedTaskLayers`) but not rename or move it.
        agent_service: Compose service the CLI's bash tool execs into
            — the wire target the driver must know so the ``bash``
            :class:`~tolokaforge.runner.models.ToolSchema` it emits
            names the right compose target.
        base_image: Image tag the adapter tags the pack's own build
            with. The driver's Dockerfile layer ``FROM``s this so its
            install layer sits on top.
        base_build_service: Companion build-only service the driver's
            layered image ``FROM``s. Ships in the compose file with a
            profile that keeps it out of ``docker compose up``.
        compose_project_prefix: Prefix the adapter bakes into the
            synthesised compose ``container_name`` template. The runner
            uses it to resolve the per-trial container the bash tool
            execs into. Empty string when the adapter has no prefix.
    """

    task_id: str
    staging_dir: Path
    compose_file: Path
    agent_service: str
    base_image: str
    base_build_service: str
    compose_project_prefix: str = ""


@dataclass
class StagedTaskLayers:
    """Contribution a driver adds on top of an adapter's :class:`StagedTask`.

    The driver returns one of these from
    :meth:`AgentDriver.apply_container_layers`; the orchestrator merges
    ``stack_requirements`` with the adapter's own
    :meth:`~tolokaforge.adapters.base.BaseAdapter.docker_stack_requirements`
    output before any trial provisions.

    Attributes:
        stack_requirements: Extra
            :class:`~tolokaforge.adapters.base.ComposeImageBuild` entries
            the driver needs the orchestrator to pre-build (typically the
            harness-layered image).
        provider_env_snapshot: The provider envelope that ended up baked
            into the compose file's agent-service ``environment:`` — the
            driver captures the values it wrote so the run's fingerprint
            can record what secret keys were installed.
    """

    stack_requirements: list[ComposeImageBuild] = field(default_factory=list)
    provider_env_snapshot: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class AgentDriver(Protocol):
    """Strategy: "how the agent produces turns during a trial".

    Two shipped implementations:

    - :class:`EngineLoopDriver` (default) — the engine's own LLM turn
      loop against a model. All methods are no-ops; adapters run
      unchanged.
    - ``CodingHarnessDriver`` (see
      :mod:`tolokaforge_coding_harnesses.driver`) — a vendor coding CLI
      in-container. Emits the ``agent_harness_command`` metadata key +
      the ``bash`` tool schema + ``test_execution`` grading defaults,
      and layers the CLI install onto the adapter's staged compose.

    Adapters never import driver code; the orchestrator holds the
    selected driver and applies it around adapter output.
    """

    name: ClassVar[str]

    def needs_container_stage(self) -> bool:
        """Does this driver need :meth:`BaseAdapter.stage_task` to return
        a real :class:`StagedTask`? Engine-loop drivers return ``False``;
        coding-harness drivers return ``True``. The orchestrator refuses
        a run whose driver needs staging on an adapter that returns
        ``None`` from ``stage_task``."""
        ...

    def needs_docker_cli(self) -> bool:
        """Does this driver need the ``docker`` CLI installed in the
        runner container? Coding-harness drivers do — the runner's
        bash-tool wrapper shells out to ``docker compose exec``.
        Engine-loop drivers do not."""
        ...

    def attach(self, adapter_name: str, staged_ok: bool) -> None:
        """Sanity-check the driver against the adapter at orchestrator
        startup. ``staged_ok`` is ``True`` iff calling
        :meth:`BaseAdapter.stage_task` on the resolved adapter returned
        a :class:`StagedTask`. Drivers that need staging raise here
        when it is ``False``, naming the adapters known to stage."""
        ...

    def decorate_task_description(
        self,
        base: TaskDescription,
        *,
        staged: StagedTask | None,
    ) -> TaskDescription:
        """Return the description as it should reach the runner.

        Engine-loop drivers return *base* unchanged. Coding-harness
        drivers rewrite tools to a single ``bash``
        :class:`~tolokaforge.runner.models.ToolSchema` targeting
        ``staged.agent_service``, override grading to
        ``test_execution``, and populate the metadata keys the
        conductor + runner branch on
        (``agent_harness_command``, ``agent_visible_dir``, ...).
        """
        ...

    def apply_container_layers(self, *, staged: StagedTask) -> StagedTaskLayers:
        """Layer this driver's needs onto the adapter's staged compose.

        Engine-loop drivers return an empty :class:`StagedTaskLayers`.
        Coding-harness drivers write the CLI install Dockerfile
        snippet under ``staged.staging_dir``, rewrite the compose
        file's agent-service ``environment:`` with the resolved
        provider envelope, add ``runner`` / ``db-service`` sidecars,
        and declare the harness-layered image build in
        ``stack_requirements``.
        """
        ...

    def close(self) -> None:
        """Release whatever ``attach()`` started. Idempotent, and safe to
        call without a prior ``attach()``.

        Engine-loop drivers hold nothing to release. Coding-harness
        drivers stop the credential-shielding gateway they may have
        launched at ``attach()`` time.
        """
        ...


class EngineLoopDriver:
    """Default driver: the engine's own LLM turn loop.

    No coding-harness metadata, no container layering, no tool-schema
    override — every method is a passthrough. This is what runs when a
    run config declares only ``models.agent.name`` (no
    ``coding_harness``).
    """

    name: ClassVar[str] = "engine_loop"

    def needs_container_stage(self) -> bool:
        return False

    def needs_docker_cli(self) -> bool:
        return False

    def attach(self, adapter_name: str, staged_ok: bool) -> None:
        del adapter_name, staged_ok

    def decorate_task_description(
        self,
        base: TaskDescription,
        *,
        staged: StagedTask | None,
    ) -> TaskDescription:
        del staged
        return base

    def apply_container_layers(self, *, staged: StagedTask) -> StagedTaskLayers:
        del staged
        return StagedTaskLayers()

    def close(self) -> None:
        return None


__all__ = [
    "AgentDriver",
    "EngineLoopDriver",
    "StagedTask",
    "StagedTaskLayers",
]
