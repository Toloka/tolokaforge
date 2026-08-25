"""Adapter-side coding-harness pattern, shared by any adapter that opts in.

An adapter routes a task to a vendor coding-agent CLI by agreeing with the
engine on four surfaces: the harness metadata the conductor branches on, the
``docker exec``-shaped tool the runner uses for exec, the ``test_execution``
grading dispatch, and the image layer that puts the CLI on ``PATH``. Every
adapter's version of those four is the same shape — only the compose /
container plumbing around them differs — so this module gives the shape one
address any adapter can inherit.

The mixin cannot import the engine: this package ships to runtimes that do
not install ``tolokaforge`` (the boundary invariant in
``tests/unit/test_package_boundary.py``). The two engine types the pattern
touches — :class:`~tolokaforge.runner.models.ToolSchema` and
:class:`~tolokaforge.runner.models.RunnerGradingConfig` — are pydantic
models, so the mixin returns payload dicts the adapter passes into the
engine constructors: ``ToolSchema(**payload)`` /
``RunnerGradingConfig(**payload)``. The dict shape mirrors the pydantic
fields, so a schema change on either engine model surfaces at the adapter's
one-line construction call, never inside the mixin.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ._registry import (
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    HarnessSpec,
    ResolvedHarnessRegistry,
    harness_command,
    resolve_effective_registry,
    validate_harness,
)
from .protocols import PathResolver

__all__ = ["CodingHarnessAdapterMixin"]


_HARNESS_INSTALL_CONTAINER_PATH = "/opt/tolokaforge/install-harness.sh"
"""Where the shipped install script lands inside the image the layer builds.

Constant because the ``RUN`` line and the ``COPY`` destination must agree,
and the install script's own layout is what the six shipped harnesses'
:attr:`HarnessSpec.install_method` values resolve against."""

_HARNESS_DOCKERFILE_NAME = "harness.Dockerfile"
"""Name of the standalone Dockerfile snippet the mixin writes into the
build context. Adapters that layer the harness on top of a task base image
pass the returned relative path to their build tooling."""


class CodingHarnessAdapterMixin:
    """Shared coding-harness pattern for adapters.

    An adapter that inherits this mixin declares — via
    :attr:`supports_coding_harness` — that the orchestrator's config gate can
    route ``models.agent.harness`` runs to it, and gets the five helpers the
    pattern needs: registry resolution, command assembly, metadata emission,
    tool-schema payload, ``test_execution`` grading payload, and the standalone
    install-script Dockerfile layer.

    The mixin is stateless — every helper takes what it needs as arguments — so
    an adapter can inherit alongside any base class without inheritance-order
    surprises."""

    supports_coding_harness: ClassVar[bool] = True
    """Adapter capability flag the orchestrator's config-validation gate reads.

    ``True`` means a run declaring ``models.agent.harness`` reaches this
    adapter; the orchestrator's gate refuses the run against any adapter whose
    flag is the ``False`` default (see
    :mod:`~tolokaforge.core.orchestrator`'s ``load_tasks``)."""

    def resolve_harness_spec(
        self,
        agent_harness: str,
        agent_model: str,
        provider_env: Mapping[str, str] | None = None,
        presets_file: str | None = None,
        plugin_discovery: bool = True,
        version_override: str | None = None,
    ) -> HarnessSpec:
        """Resolve *agent_harness* against the effective registry.

        Composes :func:`resolve_effective_registry` — shipped catalog + optional
        operator overlay + installed plugins — then :func:`validate_harness` to
        turn a typo into an actionable error naming the accepted set. Refuses
        :data:`ENGINE_LOOP` (which runs no CLI, so no spec exists) and refuses
        an empty *agent_model* for a real harness (the CLI's own default would
        drive the trial otherwise, silently unpinning the model under
        measurement).

        *provider_env* is not consulted here — its keys drive command assembly
        and are validated then; it remains on the signature so an adapter can
        forward the run's envelope in one place.

        *version_override* — when set (typically from
        ``models.agent.harness_version`` or the ``name@version`` slug shape on
        ``models.agent.harness``), the resolved spec's ``version`` is replaced
        with this value. The rest of the spec is untouched, so ``install_source``
        + ``install_method`` still target the vendor CLI's release channel and
        ``install-harness.sh`` installs the requested version at trial-image
        build time. Deviating from the shipped pin trades reproducibility for
        flexibility: the recorded trial artefact reflects the override so replay
        can see it, but two operators running the same run config with different
        overrides get different scores.

        Raises:
            ValueError: *agent_harness* is unknown, is :data:`ENGINE_LOOP`,
                *agent_model* is empty for a real harness, or *version_override*
                is the empty string.
        """
        del provider_env  # forwarded by adapters; validated at command-assembly time
        resolved: ResolvedHarnessRegistry = resolve_effective_registry(
            presets_file, discover_plugins=plugin_discovery
        )
        validate_harness(agent_harness, resolved.harnesses)
        spec = resolved.harnesses.get(agent_harness)
        if spec is None:
            raise ValueError(
                f"coding harness: agent_harness {agent_harness!r} runs no CLI (the engine "
                "loop drives the trial through the engine's own LLM turn loop); "
                "resolve_harness_spec is for CLI-mode runs only."
            )
        if not agent_model:
            raise ValueError(
                f"coding harness: agent_harness {agent_harness!r} requires a non-empty "
                "agent_model — the CLI selects its own default otherwise, so the run "
                "config's model would not be the one measured."
            )
        if version_override is not None:
            if not version_override:
                raise ValueError(
                    f"coding harness: agent_harness {agent_harness!r} version_override "
                    "must be non-empty when set; drop the field to use the shipped pin."
                )
            spec = spec.model_copy(update={"version": version_override})
        return spec

    def build_harness_command(
        self,
        agent_harness: str,
        spec: HarnessSpec,
        instruction: str,
        model: str,
        provider_env: Mapping[str, str] | None = None,
        *,
        path_resolver: PathResolver | None = None,
    ) -> str:
        """Shell command that runs *agent_harness*'s CLI against *instruction*.

        Thin wrapper around :func:`harness_command` — kept on the mixin so the
        adapter code reads as "resolve → build → emit" and doesn't need to
        thread the harness registry through.

        The command reaches ``bash -c`` inside the task container via the
        exec-wrapper the payload from :meth:`emit_harness_tool_schema`
        configures; every argv token is already shell-quoted upstream."""
        return harness_command(
            agent_harness,
            instruction,
            model,
            registry={agent_harness: spec},
            provider_env=provider_env,
            path_resolver=path_resolver,
        )

    def emit_harness_metadata(
        self,
        agent_harness: str,
        spec: HarnessSpec,
        command: str,
        model: str,
    ) -> dict[str, Any]:
        """Metadata handshake the conductor branches on.

        Four keys — the conductor's ``_run_agent_loop`` reads
        ``agent_harness_command`` and dispatches; the three siblings are
        recorded on the trajectory so replay can reconstruct which CLI + version
        + model produced the trial. Adapters that carry their own task-specific
        metadata (difficulty, tags, timeouts) merge those in around this dict.
        """
        return {
            "agent_harness": agent_harness,
            "agent_harness_version": spec.version,
            "agent_harness_model": model,
            "agent_harness_command": command,
        }

    def emit_harness_tool_schema(
        self,
        *,
        service: str,
        compose_project_prefix: str,
        timeout_s: float,
        toolset: str = "coding_harness",
    ) -> dict[str, Any]:
        """Payload the adapter passes to :class:`~tolokaforge.runner.models.ToolSchema`.

        Shape matches the pydantic model field-for-field; nested ``source`` is
        the :class:`~tolokaforge.runner.models.ToolSource` payload, with
        ``invocation_style`` as the ``docker_compose_exec`` enum value string so
        the engine's :class:`~tolokaforge.runner.models.InvocationStyle` string
        enum reconstructs it.

        *service* and *compose_project_prefix* land on ``source.extra`` — the
        two fields the runner's
        :class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper`
        reads to resolve the container name at start time. *timeout_s* has to
        carry the whole trial's agent budget under harness mode: the CLI runs
        to completion inside a single ``exec`` — the trial has no LLM turn loop
        to time-slice.

        The adapter constructs the engine type with ``ToolSchema(**payload)``;
        pydantic v2 instantiates the nested ``ToolSource`` from the sub-dict.
        """
        return {
            "name": "bash",
            "description": "Execute a bash command inside the task container",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run",
                    }
                },
                "required": ["command"],
            },
            "category": "compute",
            "timeout_s": timeout_s,
            "source": {
                "toolset": toolset,
                "module_path": "",
                "class_name": "bash",
                "invocation_style": "docker_compose_exec",
                "extra": {
                    "service": service,
                    "compose_project_prefix": compose_project_prefix,
                },
            },
        }

    def emit_test_execution_grading(self) -> dict[str, Any]:
        """Payload the adapter passes to :class:`~tolokaforge.runner.models.RunnerGradingConfig`.

        Fixed shape: the runner dispatches on ``grading_method="test_execution"``
        and scores the trial by reading the reward the task's own verifier
        wrote to ``/logs/verifier/reward.txt``. Weights and threshold match the
        historical terminal-bench values so a run switching to the mixin scores
        byte-identically."""
        return {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "grading_method": "test_execution",
        }

    def write_install_script_layer(
        self,
        context_dir: Path,
        base_image: str,
        spec: HarnessSpec,
        middleware_proxy: bool = False,
    ) -> str:
        """Materialise a standalone image-layer build context for *spec*.

        Writes three files into *context_dir* (which the adapter has already
        created — the mixin does not own the caller's staging tree):

        - ``install-harness.sh`` — the shipped installer, copied verbatim.
        - :attr:`_HARNESS_DOCKERFILE_NAME` — a Dockerfile snippet that ``FROM``
          *base_image*, ``COPY``s the installer to
          :data:`_HARNESS_INSTALL_CONTAINER_PATH`, and ``RUN``s it against
          :attr:`HarnessSpec.install_method` / :attr:`HarnessSpec.install_source`
          / :attr:`HarnessSpec.version`.
        - ``middleware_proxy.py`` — the shipped proxy, only when *middleware_proxy*
          is ``True`` (typically because ``spec.request_middleware`` is set).
          The Dockerfile gains one more ``COPY`` line so the proxy lands at
          :data:`MIDDLEWARE_PROXY_CONTAINER_PATH` inside the image.

        The build context is self-contained: paths in the Dockerfile are
        relative to *context_dir*, not to the adapter's surrounding compose or
        staging tree. An adapter with compose-specific plumbing (a
        ``.dockerignore`` at the compose context root, a nested build-context
        location) wraps that around this snippet.

        Returns:
            The Dockerfile's path relative to *context_dir* — the caller's
            build tooling reads it as the ``dockerfile:`` argument.
        """
        install_source = INSTALL_SCRIPT.name
        shutil.copy2(INSTALL_SCRIPT, context_dir / install_source)
        lines = [
            f"FROM {base_image}",
            f"COPY {install_source} {_HARNESS_INSTALL_CONTAINER_PATH}",
            f"RUN sh {_HARNESS_INSTALL_CONTAINER_PATH} {spec.install_method} "
            f"{shlex.quote(spec.install_source)} {shlex.quote(spec.version)}",
        ]
        if middleware_proxy:
            middleware_source = MIDDLEWARE_PROXY_SCRIPT.name
            shutil.copy2(MIDDLEWARE_PROXY_SCRIPT, context_dir / middleware_source)
            lines.append(f"COPY {middleware_source} {MIDDLEWARE_PROXY_CONTAINER_PATH}")
        (context_dir / _HARNESS_DOCKERFILE_NAME).write_text("\n".join(lines) + "\n")
        return _HARNESS_DOCKERFILE_NAME
