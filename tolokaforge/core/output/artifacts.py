"""Trial artifact writer — Protocol + disk-backed implementation.

The writer composes :class:`tolokaforge.core.output_writer.OutputWriter`
for the seven per-trial YAML files that make up a trial bundle:

* ``task.yaml`` — frozen task identity + resolved preset fingerprint
* ``trajectory.yaml`` — full message trace incl. reasoning blocks
* ``env.yaml`` — final env state snapshot
* ``metrics.yaml`` — usage / latency / tool-call metrics
* ``grade.yaml`` — pass / fail + score components
* ``logs.yaml`` — structured trial logs
* ``tools_schemas.yaml`` — post-policy tool list, what the provider saw

Every artifact is YAML, every artifact is per-trial — a trial bundle is
self-contained, no sidecar lookup needed for audit. Disk overhead is
small enough that dedup across the 5 repeats × N tasks isn't worth the
indirection.

* :class:`TrialArtifactWriter` — Protocol the orchestrator depends on.
* :class:`FileArtifactWriter` — disk-backed implementation.
* :func:`model_id_slug` — deterministic filesystem-safe slug combining
  provider + model name (still used by other call-sites; kept here as
  the canonical helper).

See [`docs/OUTPUT_FORMAT.md`](../../../docs/OUTPUT_FORMAT.md) for the full
authoritative on-disk contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from tolokaforge.core.output_writer import OutputWriter

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import Grade, Trajectory

__all__ = [
    "FileArtifactWriter",
    "TrialArtifactWriter",
    "model_id_slug",
]


# ---------------------------------------------------------------------------
# model_id slug utility
# ---------------------------------------------------------------------------

# Characters safe in POSIX + Windows filenames. The slug allows letters,
# digits, dot, underscore, dash. Any other character (``/``, ``:``, ``@``,
# whitespace, …) collapses to a single ``_``.
_SAFE_RUN = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_component(text: str) -> str:
    """Collapse filesystem-hostile characters in *text* to single underscores."""
    if not text:
        return ""
    # Normalise: collapse ``/`` first (common in provider-scoped model names
    # like ``anthropic/claude-opus-4.7``) then any remaining unsafe runs.
    collapsed = _SAFE_RUN.sub("_", text)
    return collapsed.strip("_") or "_"


def model_id_slug(provider: str, name: str) -> str:
    """Deterministic filesystem-safe slug combining *provider* and *name*.

    The slug shows up in any artifact path that needs a filesystem-safe
    model identifier (cache file names, debug-dump filenames). The
    ``__`` separator survives any single-underscore collapse inside each
    component so ``provider`` / ``name`` boundary stays parseable.

    Rules
    -----
    * Replace all characters outside ``[A-Za-z0-9._-]`` with ``_``.
    * Collapse runs of unsafe characters to a single ``_``.
    * Strip leading / trailing ``_`` from each component.
    * Join with ``__`` (double underscore).

    Examples
    --------
    >>> model_id_slug("openrouter", "anthropic/claude-opus-4.7")
    'openrouter__anthropic_claude-opus-4.7'
    >>> model_id_slug("openai", "gpt-5.5")
    'openai__gpt-5.5'
    >>> model_id_slug("nova", "amazon-nova-micro-1_0")
    'nova__amazon-nova-micro-1_0'
    >>> model_id_slug("", "some-model")
    'unknown__some-model'
    """
    provider_slug = _slug_component(provider) or "unknown"
    name_slug = _slug_component(name) or "unnamed"
    return f"{provider_slug}__{name_slug}"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TrialArtifactWriter(Protocol):
    """Writes per-trial artifacts + per-(task, model) sidecars.

    The orchestrator depends on this Protocol, not
    :class:`FileArtifactWriter`. Alternative implementations (in-memory for
    tests, remote-object-store for production, …) satisfy the same contract.
    """

    def write_trajectory(self, trial_dir: Path, trajectory: Trajectory) -> None:
        """Persist ``trial_dir/trajectory.yaml`` from *trajectory*."""
        ...

    def write_task(self, trial_dir: Path, task_snapshot: dict[str, Any]) -> None:
        """Persist ``trial_dir/task.yaml`` from a serialisable dict."""
        ...

    def write_env(self, trial_dir: Path, env_state: dict[str, Any]) -> None:
        """Persist ``trial_dir/env.yaml`` from a serialisable dict."""
        ...

    def write_metrics(self, trial_dir: Path, trajectory: Trajectory) -> None:
        """Persist ``trial_dir/metrics.yaml`` from *trajectory.metrics*."""
        ...

    def write_grade(self, trial_dir: Path, grade: Grade) -> None:
        """Persist ``trial_dir/grade.yaml`` from *grade*."""
        ...

    def write_logs(self, trial_dir: Path, logger: StructuredLogger) -> None:
        """Persist ``trial_dir/logs.yaml`` from *logger*."""
        ...

    def write_tools_schemas(
        self,
        trial_dir: Path,
        schemas: list[dict[str, Any]],
    ) -> None:
        """Write ``trial_dir/tools_schemas.yaml`` from *schemas*.

        *schemas* is the post-policy tool list — what
        ``capabilities.schema_sanitizer.sanitize(...)`` returned, ie. exactly
        what the provider saw on the wire. Writing it inside the trial
        bundle alongside ``trajectory.yaml`` keeps every trial
        self-contained for audit. No dedup, no cross-trial state.
        """
        ...

    def write_prompts(
        self,
        trial_dir: Path,
        agent_prompt: str | None,
        user_prompt: str | None,
    ) -> None:
        """Write ``trial_dir/prompts.yaml`` from the per-trial system prompts.

        Pulls the agent's effective system prompt (post-prompt-policy
        enrichment) and the user simulator's system prompt out of
        ``trajectory.yaml`` so the trajectory carries only the message
        trace + status + metrics. Field names match the historical
        ``Trajectory.system_prompt`` / ``Trajectory.user_system_prompt``
        keys so analytics tools that already read those names keep
        working — only the file moved.
        """
        ...


# ---------------------------------------------------------------------------
# FileArtifactWriter — disk-backed implementation
# ---------------------------------------------------------------------------


class FileArtifactWriter:
    """Disk-backed :class:`TrialArtifactWriter`.

    Per-trial writes compose an :class:`OutputWriter` keyed on ``trial_dir``
    (cached so repeated calls for the same trial don't churn directory
    creation). Every artifact — including the post-policy
    ``tools_schemas.yaml`` — lives inside the trial bundle for
    self-contained audit.
    """

    def __init__(self) -> None:
        # Cache per-trial :class:`OutputWriter` instances — creating one
        # ``mkdir -p``s the trial directory, cheap but pointless to repeat.
        self._trial_writers: dict[Path, OutputWriter] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _writer(self, trial_dir: Path) -> OutputWriter:
        key = Path(trial_dir).resolve()
        if key not in self._trial_writers:
            self._trial_writers[key] = OutputWriter(trial_dir)
        return self._trial_writers[key]

    # ------------------------------------------------------------------
    # Per-trial writes — thin delegation to the existing OutputWriter API
    # ------------------------------------------------------------------

    def write_trajectory(self, trial_dir: Path, trajectory: Trajectory) -> None:
        self._writer(trial_dir).write_trajectory(trajectory)

    def write_task(self, trial_dir: Path, task_snapshot: dict[str, Any]) -> None:
        self._writer(trial_dir).write_task_info(task_snapshot)

    def write_env(self, trial_dir: Path, env_state: dict[str, Any]) -> None:
        self._writer(trial_dir).write_env_state(env_state)

    def write_metrics(self, trial_dir: Path, trajectory: Trajectory) -> None:
        self._writer(trial_dir).write_metrics(trajectory)

    def write_grade(self, trial_dir: Path, grade: Grade) -> None:
        self._writer(trial_dir).write_grade(grade)

    def write_logs(self, trial_dir: Path, logger: StructuredLogger) -> None:
        self._writer(trial_dir).write_logs(logger)

    # ------------------------------------------------------------------
    # Convenience wrapper matching the old OutputWriter.write_all
    # ------------------------------------------------------------------

    def write_trial_bundle(
        self,
        trial_dir: Path,
        trajectory: Trajectory,
        task_snapshot: dict[str, Any],
        env_state: dict[str, Any],
        logger: StructuredLogger,
    ) -> None:
        """Write task + trajectory + env + metrics + grade + logs for a trial."""
        writer = self._writer(trial_dir)
        writer.write_all(trajectory, task_snapshot, env_state, logger)

    # ------------------------------------------------------------------
    # tools_schemas — per-trial YAML inside the trial bundle
    # ------------------------------------------------------------------

    def write_tools_schemas(
        self,
        trial_dir: Path,
        schemas: list[dict[str, Any]],
    ) -> None:
        """Write ``trial_dir/tools_schemas.yaml`` from *schemas*.

        *schemas* is the post-policy tool list — what
        ``capabilities.schema_sanitizer.sanitize(...)`` returned, ie. exactly
        what the provider saw. Storing it inside the trial bundle keeps
        every trial self-contained for audit (no cross-trial lookup),
        matches the all-YAML convention of the rest of the bundle, and
        lets tooling that already reads
        ``trial_dir / "trajectory.yaml"`` pick up the schema with the
        same loader.

        Overwrites unconditionally — the trial directory is always created
        fresh by the orchestrator, so there is no stale-write concern.
        """
        trial_dir = Path(trial_dir)
        trial_dir.mkdir(parents=True, exist_ok=True)
        target = trial_dir / "tools_schemas.yaml"
        with open(target, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                schemas,
                f,
                sort_keys=True,
                allow_unicode=True,
                default_flow_style=False,
            )

    # ------------------------------------------------------------------
    # prompts — per-trial YAML inside the trial bundle
    # ------------------------------------------------------------------

    def write_prompts(
        self,
        trial_dir: Path,
        agent_prompt: str | None,
        user_prompt: str | None,
    ) -> None:
        """Write ``trial_dir/prompts.yaml`` from the per-trial prompts.

        ``trajectory.yaml`` carries only the message trace + status +
        metrics; the much larger system prompts (often 15–20 KB on
        domain-rich evals) live here instead. Field names mirror the
        historical ``Trajectory.system_prompt`` / ``Trajectory.user_system_prompt``
        so external readers don't have to learn new names — the file
        moved, the keys did not.

        Both arguments may be ``None`` (no agent system prompt set, or
        scripted user simulator with no LLM-shaped prompt). The keys are
        always present so consumers can distinguish *absent* (``None``)
        from *missing field* (file not yet written).
        """
        trial_dir = Path(trial_dir)
        trial_dir.mkdir(parents=True, exist_ok=True)
        target = trial_dir / "prompts.yaml"
        payload = {
            "system_prompt": agent_prompt,
            "user_system_prompt": user_prompt,
        }
        with open(target, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                payload,
                f,
                sort_keys=True,
                allow_unicode=True,
                default_flow_style=False,
            )
