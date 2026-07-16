"""``OpenAgentLoopManager`` — the run-scoped coordinator for the OAL gate.

Owns the concerns that used to live on :class:`Orchestrator` when open mode
is on:

* the per-run :class:`SessionRegistry` (one live :class:`InProcessTrialSession`
  per trial keyed by ``trial_id``),
* the per-trial ``observer_provider`` / ``intervention_handler_provider``
  closures the orchestrator threads through :class:`ConductorContext`,
* the trace-write hook that persists ``open_agent_loop.yaml`` at each trial
  completion.

Passed to :class:`Orchestrator` via ``OrchestratorDeps.oal_manager``. The
orchestrator's contract remains *"give me a config + deps, I run trials"* —
session lifecycles, participant provisioning, and trace writing are handled
by whichever manager the caller supplied (or, for user-ergonomic backward
compatibility, one the orchestrator auto-constructs from
``config.open_agent_loop.enabled``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.session.in_process import InProcessTrialSession
from tolokaforge.session.intervention_pump import SessionInterventionHandler
from tolokaforge.session.loop_observer import SessionLoopObserver
from tolokaforge.session.registry import SessionRegistry

if TYPE_CHECKING:
    from tolokaforge.core.models import RunConfig

__all__ = ["OpenAgentLoopManager"]


class OpenAgentLoopManager:
    """Run-scoped coordinator for the Open Agent Loop gate.

    Instantiated once per :class:`~tolokaforge.core.orchestrator.Orchestrator`
    run (when open mode is on) — either explicitly by the caller via
    ``OrchestratorDeps.oal_manager`` or implicitly by the orchestrator's
    backward-compat fallback from
    :meth:`~tolokaforge.core.orchestrator.Orchestrator.__init__` when
    ``config.open_agent_loop.enabled`` is true.

    Owns:

    * :attr:`sessions` — the :class:`SessionRegistry`. External participants
      look sessions up here to attach.
    * :meth:`observer_provider` / :meth:`intervention_handler_provider` —
      the two per-trial factories threaded through
      :class:`~tolokaforge.core.conductor.ConductorContext`.
    * :meth:`write_trace` — persists a trial's ``open_agent_loop.yaml``
      companion file alongside ``trajectory.yaml``.

    Does **not** own:

    * The loop-side seams (``LoopObserver`` / ``InterventionHandler``) — those
      live on :mod:`tolokaforge.core.loop` and this manager just supplies
      the session-bound implementations.
    * The trial-completion loop — the orchestrator calls
      :meth:`write_trace` after each ``future.result()``.
    """

    def __init__(self) -> None:
        self._session_registry = SessionRegistry()

    @property
    def sessions(self) -> SessionRegistry:
        """Live-session registry for the run. External participants (M2 CLI
        attach, cross-trial orchestrator) look sessions up here by
        ``trial_id``.
        """
        return self._session_registry

    def observer_provider(self) -> Callable[[str], SessionLoopObserver | None]:
        """Return the per-trial observer factory for the current run.

        Closes over the internal registry. When called with a ``trial_id``,
        gets-or-creates the trial's session and returns a fresh
        :class:`SessionLoopObserver` bound to it. Threadsafe under the
        registry's own lock; each trial's conductor runs on its own worker
        thread and receives its own observer instance.
        """
        registry = self._session_registry

        def _provider(trial_id: str) -> SessionLoopObserver | None:
            session: InProcessTrialSession = registry.get_or_create(trial_id)
            return SessionLoopObserver(session)

        return _provider

    def intervention_handler_provider(
        self,
    ) -> Callable[[str], SessionInterventionHandler | None]:
        """Return the per-trial intervention-handler factory for the current run.

        Symmetric to :meth:`observer_provider`. Same registry — the observer
        and the handler bind to the same session per trial, so events and
        interventions round-trip through one bus.
        """
        registry = self._session_registry

        def _provider(trial_id: str) -> SessionInterventionHandler | None:
            session: InProcessTrialSession = registry.get_or_create(trial_id)
            return SessionInterventionHandler(session)

        return _provider

    def write_trace(self, output_dir: Path, task_id: str, trial_idx: int) -> None:
        """Persist the trial's ``open_agent_loop.yaml`` snapshot to disk.

        No-op when the trial never entered the run body (no session created
        for it). Writes ``trials/<task_id>/<trial_idx>/open_agent_loop.yaml``
        alongside ``trajectory.yaml``. Companion-file shape (not merged into
        ``trajectory.yaml``) keeps the ``Trajectory`` model unchanged and
        canonical snapshot tests undisturbed.

        Write failures raise :class:`OSError` — callers (orchestrator's
        trial-completion hook) catch, log, and continue so trace writes never
        mask a live-trial result.
        """
        session = self._session_registry.get(f"{task_id}:{trial_idx}")
        if session is None:
            return
        import yaml

        trace_path = output_dir / "trials" / task_id / str(trial_idx) / "open_agent_loop.yaml"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("w") as fh:
            yaml.safe_dump(session.snapshot(), fh, sort_keys=False)

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        """Snapshot every session in the registry.

        For diagnostics + programmatic post-run analysis (a script that wants
        every open-mode trial's trace without walking the filesystem).
        """
        return {
            trial_id: self._session_registry.get_or_create(trial_id).snapshot()
            for trial_id in self._session_registry.all_trial_ids()
        }

    @classmethod
    def from_config(cls, config: RunConfig) -> OpenAgentLoopManager | None:
        """Build a manager when ``config.open_agent_loop.enabled`` is true,
        else ``None``.

        The user-ergonomic entrypoint — CLI code that reads ``RunConfig``
        can call this and pass the result (possibly ``None``) into
        ``OrchestratorDeps``. The orchestrator's backward-compat fallback
        also uses this classmethod when no manager was supplied in deps,
        so flipping ``open_agent_loop.enabled: true`` in a YAML config
        still activates the gate without any Python wiring changes.
        """
        if config.open_agent_loop is None or not config.open_agent_loop.enabled:
            return None
        return cls()
