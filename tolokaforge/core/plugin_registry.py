"""Fail-loud entry-point registries for the swappable seams.

External code discovers and loads alternative implementations of the
:class:`~tolokaforge.core.runtime.RuntimeBackend`,
:class:`~tolokaforge.core.trial_grader.TrialGrader`,
:class:`~tolokaforge.core.conductor.Conductor`,
:class:`~tolokaforge.core.service_readiness.ServiceReadinessProbe`,
:class:`~tolokaforge.core.actors.turn_policy.TurnPolicy`,
:class:`~tolokaforge.core.grading.substrate.GradingSubstrate`,
:class:`~tolokaforge.core.grading.check_runner.CheckExecutor`, and
:class:`~tolokaforge.core.grading.judge_model_provider.JudgeModelProvider`
Protocols through ``importlib.metadata`` entry-point groups — no in-tree
edit, no monkey-patch. Each entry point resolves to a *factory callable*,
mirroring the existing :data:`~tolokaforge.core.conductor.ConductorFactory`
idiom. Four of the seams adapt divergent impl constructors to a per-group
frozen-dataclass context (``Callable[[<Context>], <Impl>]``); the readiness
probes, custom-check executors, and judge model providers need no build
dependencies, so their factories are arg-less (``Callable[[], <Impl>]``).
The grading-substrate loader resolves directly to the ``GradingSubstrate``
implementation *class* — the substrate seam is constructed per-trial with
topology-specific arguments the plug-in group cannot generically supply,
so callers instantiate the returned class themselves (see ADR-0039).

The groups:

* ``tolokaforge.runtime_backends`` → :data:`RuntimeBackendFactory`
* ``tolokaforge.trial_graders`` → :data:`TrialGraderFactory`
* ``tolokaforge.conductors`` → :data:`~tolokaforge.core.conductor.ConductorFactory`
* ``tolokaforge.service_readiness_probes`` → :data:`ReadinessProbeFactory`
* ``tolokaforge.turn_policies`` → :data:`TurnPolicyFactory`
* ``tolokaforge.grading_substrates`` → ``type[GradingSubstrate]``
* ``tolokaforge.custom_check_executors`` → :data:`CustomCheckExecutorFactory`
* ``tolokaforge.judge_model_providers`` → :data:`JudgeModelProviderFactory`

Discovery is lazy and cached per group; it enumerates ``ep.name`` /
``ep.dist`` **without** calling ``ep.load()``. This splits the fail-loud
policy into two shapes:

* **Duplicate name** — an unresolvable ambiguity — fails at discovery for
  *any* lookup into that group, naming both providing distributions.
* **Broken import** fails only when its own name is requested; a broken
  plug-in never breaks resolution of a healthy sibling, and the import
  error propagates loudly rather than being swallowed.

External code and other engine registries reuse this fail-loud discovery
through :func:`discover_entry_points`; see the § Fail-loud registry pattern
subsection in ``docs/ADAPTER_ARCHITECTURE.md`` for the required shape.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from tolokaforge.core.actors.turn_policy import TurnPolicy
from tolokaforge.core.conductor import ConductorFactory
from tolokaforge.core.grading.check_runner import CheckExecutor
from tolokaforge.core.grading.judge_model_provider import JudgeModelProviderFactory
from tolokaforge.core.grading.substrate import GradingSubstrate
from tolokaforge.core.models.run_config import GraderConfig
from tolokaforge.core.run_display_events import RunDisplayEvents, _NullRunDisplayEvents
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.service_readiness import ServiceReadinessProbe
from tolokaforge.core.trial_grader import TrialGrader

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

    from tolokaforge.core.actors.actor import Actor
    from tolokaforge.core.compose_materialisation import LogCaptureConfig
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import SeedRef
    from tolokaforge.core.trial import EnvironmentManifest

__all__ = [
    "ConductorFactory",
    "CustomCheckExecutorFactory",
    "DuplicateRegistrationError",
    "JudgeModelProviderFactory",
    "ReadinessProbeFactory",
    "RegistryError",
    "RuntimeBackendBuildContext",
    "RuntimeBackendFactory",
    "TrialGraderContext",
    "TrialGraderFactory",
    "TurnPolicyContext",
    "TurnPolicyFactory",
    "UnknownImplementationError",
    "available_conductors",
    "available_custom_check_executors",
    "available_grading_substrates",
    "available_judge_model_providers",
    "available_readiness_probes",
    "available_runtime_backends",
    "available_trial_graders",
    "available_turn_policies",
    "discover_entry_points",
    "load_conductor",
    "load_custom_check_executor",
    "load_grading_substrate",
    "load_judge_model_provider",
    "load_readiness_probe",
    "load_runtime_backend",
    "load_trial_grader",
    "load_turn_policy",
]

RUNTIME_BACKENDS_GROUP = "tolokaforge.runtime_backends"
TRIAL_GRADERS_GROUP = "tolokaforge.trial_graders"
CONDUCTORS_GROUP = "tolokaforge.conductors"
SERVICE_READINESS_PROBES_GROUP = "tolokaforge.service_readiness_probes"
TURN_POLICIES_GROUP = "tolokaforge.turn_policies"
GRADING_SUBSTRATES_GROUP = "tolokaforge.grading_substrates"
CUSTOM_CHECK_EXECUTORS_GROUP = "tolokaforge.custom_check_executors"
JUDGE_MODEL_PROVIDERS_GROUP = "tolokaforge.judge_model_providers"


# ---------------------------------------------------------------------------
# Fail-loud error surface
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base class for every entry-point registry failure."""


class UnknownImplementationError(RegistryError):
    """No entry point named ``name`` is registered in ``group``.

    The message lists every known registered name so the operator can spot a
    typo or a missing ``pip install`` without reading source.
    """

    def __init__(self, name: str, group: str, known: list[str]) -> None:
        self.name = name
        self.group = group
        self.known = known
        known_str = ", ".join(known) if known else "(none registered)"
        super().__init__(
            f"Unknown implementation {name!r} in entry-point group {group!r}. "
            f"Known names: {known_str}. "
            "Install the providing package or check the configured name."
        )


class DuplicateRegistrationError(RegistryError):
    """Two entry points share ``name`` within ``group``.

    An unresolvable ambiguity: the loader cannot safely pick one for anyone, so
    it fails every lookup into the group and names both providing distributions.
    """

    def __init__(self, name: str, group: str, distributions: tuple[str, str]) -> None:
        self.name = name
        self.group = group
        self.distributions = distributions
        first, second = distributions
        super().__init__(
            f"Duplicate registration of {name!r} in entry-point group {group!r}: "
            f"provided by both {first!r} and {second!r}. "
            "Uninstall or rename one to resolve the ambiguity."
        )


# ---------------------------------------------------------------------------
# Seam types — per-group contexts + factory aliases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeBackendBuildContext:
    """Everything a runtime-backend factory may need to build its backend.

    The union of what any built-in needs — a factory is free to ignore fields
    it does not use (the ``in_memory`` factory reads none of them).
    """

    runner_address: str
    env_manifest: EnvironmentManifest | None
    run_id: str
    seeds: dict[str, SeedRef]
    log_capture: LogCaptureConfig | None
    events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)
    mount_docker_socket: bool = False
    """Bind-mount the host docker socket into the materialised runner. Set by
    the same predicate that decides whether to bake the docker CLI into the
    runner image (see :func:`_run_needs_docker_cli`): the terminal-bench
    adapter (which shells out to docker directly against the host daemon) or
    any run routing a shipped tool through the compose variant
    (``tools.agent.<tool>.service``), whose wrappers ``docker exec`` from the
    runner into a sibling service. CLI without socket, or socket without CLI,
    are both useless — the two flags are one decision."""


@dataclass(frozen=True)
class TrialGraderContext:
    """Serialisable configuration a trial-grader factory receives from the orchestrator.

    Carries only data, not live objects: two endpoint strings, the run-scoped
    logger, and the optional ``grader`` config block. A live gRPC channel
    would couple the grader to the orchestrator's chosen runner instance
    and block a grader that runs on a different machine — precisely the
    coupling the plug-in seam exists to break (see ADR-0038).

    :attr:`runner_address` is the runner service's gRPC address; the
    ``runner_rpc`` grader dials it.

    :attr:`grader_address` is the standalone grader service's gRPC address;
    the ``grader_rpc`` grader dials it. Defaults to ``None`` when the
    operator hasn't split the two — factories that need a grader-specific
    endpoint fall back to :attr:`runner_address` in that case, keeping
    single-address deployments trivially compatible.

    :attr:`grader_config` is the run's ``RunConfig.grader`` block (or
    ``None`` when the operator declared none). Transport-specific factories
    read their own subblock — ``queue`` reads ``grader_config.queue`` for
    worker-pool sizing and the downstream ``worker_grader`` name.
    """

    runner_address: str | None
    logger: StructuredLogger
    grader_address: str | None = None
    grader_config: GraderConfig | None = None


@dataclass(frozen=True)
class TurnPolicyContext:
    """Dependencies a turn-policy factory receives from the runner.

    ``user_simulator`` is ``None`` for policies that dispatch no user actor
    (the agent-monologue case); the conversational factory refuses that
    combination loudly rather than papering over it with a scripted stub.
    """

    user_simulator: Actor | None = None


RuntimeBackendFactory = Callable[[RuntimeBackendBuildContext], RuntimeBackend]
TrialGraderFactory = Callable[[TrialGraderContext], TrialGrader]
ReadinessProbeFactory = Callable[[], ServiceReadinessProbe]
TurnPolicyFactory = Callable[[TurnPolicyContext], TurnPolicy]
CustomCheckExecutorFactory = Callable[[], CheckExecutor]


# ---------------------------------------------------------------------------
# Discovery — one generic scan + resolve shared across the groups
# ---------------------------------------------------------------------------

_discovery_cache: dict[str, dict[str, EntryPoint]] = {}


def _distribution_name(ep: EntryPoint) -> str:
    dist = ep.dist
    return dist.name if dist is not None else "<unknown distribution>"


def discover_entry_points(group: str) -> Mapping[str, EntryPoint]:
    """Return a cached ``name → EntryPoint`` read-only mapping for ``group``.

    Enumerates names and distributions without importing any target
    (``ep.load()`` is deferred to the caller). Raises
    :class:`DuplicateRegistrationError` before caching, so a group with a
    duplicate re-raises on every lookup rather than caching a partial map.

    This is the canonical fail-loud discovery primitive for every engine-side
    ``importlib.metadata`` entry-point registry; consumers iterate the returned
    mapping and load each entry point at their own point of use. The return is
    the internal cache by reference — callers must not mutate it; copy first
    (``dict(mapping)``) if a mutable view is needed.
    """
    cached = _discovery_cache.get(group)
    if cached is not None:
        return cached

    mapping: dict[str, EntryPoint] = {}
    for ep in importlib.metadata.entry_points(group=group):
        existing = mapping.get(ep.name)
        if existing is not None:
            raise DuplicateRegistrationError(
                ep.name,
                group,
                (_distribution_name(existing), _distribution_name(ep)),
            )
        mapping[ep.name] = ep

    _discovery_cache[group] = mapping
    return mapping


def _load(group: str, name: str) -> object:
    """Resolve ``name`` in ``group`` to its factory, importing the target now.

    An unknown name raises :class:`UnknownImplementationError`; a target that
    raises on ``.load()`` propagates that exception unchanged.
    """
    mapping = discover_entry_points(group)
    ep = mapping.get(name)
    if ep is None:
        raise UnknownImplementationError(name, group, sorted(mapping))
    return ep.load()


def _clear_discovery_cache() -> None:
    """Drop the cached per-group maps so the next lookup re-scans.

    The registries cache discovery on first scan; tests that inject different
    entry-point sets call this to restore isolation between cases.
    """
    _discovery_cache.clear()


# ---------------------------------------------------------------------------
# Public loaders + name listings
# ---------------------------------------------------------------------------


def load_runtime_backend(name: str) -> RuntimeBackendFactory:
    """Resolve a registered runtime-backend name to its factory callable."""
    return cast(RuntimeBackendFactory, _load(RUNTIME_BACKENDS_GROUP, name))


def load_trial_grader(name: str) -> TrialGraderFactory:
    """Resolve a registered trial-grader name to its factory callable."""
    return cast(TrialGraderFactory, _load(TRIAL_GRADERS_GROUP, name))


def load_conductor(name: str) -> ConductorFactory:
    """Resolve a registered conductor name to its factory callable."""
    return cast(ConductorFactory, _load(CONDUCTORS_GROUP, name))


def load_readiness_probe(kind: str) -> ReadinessProbeFactory:
    """Resolve a registered readiness-probe kind to its factory callable."""
    return cast(ReadinessProbeFactory, _load(SERVICE_READINESS_PROBES_GROUP, kind))


def load_turn_policy(name: str) -> TurnPolicyFactory:
    """Resolve a registered turn-policy name to its factory callable."""
    return cast(TurnPolicyFactory, _load(TURN_POLICIES_GROUP, name))


def load_custom_check_executor(name: str) -> CustomCheckExecutorFactory:
    """Resolve a registered custom-check-executor name to its factory callable."""
    return cast(CustomCheckExecutorFactory, _load(CUSTOM_CHECK_EXECUTORS_GROUP, name))


def load_judge_model_provider(name: str) -> JudgeModelProviderFactory:
    """Resolve a registered judge-model-provider name to its factory callable."""
    return cast(JudgeModelProviderFactory, _load(JUDGE_MODEL_PROVIDERS_GROUP, name))


def load_grading_substrate(name: str) -> type[GradingSubstrate]:
    """Resolve a registered grading-substrate name to its implementation class.

    Unlike the other loaders — which return a factory callable adapting a
    per-group context to an impl — this one returns the substrate *class*
    itself. Substrates are constructed per-trial with topology-specific
    arguments (runner address + trial id for ``live_callback``; live
    ``db_reader`` / factories for ``in_process``; a snapshot bundle for
    future ``snapshot``) that no shared context can generically supply, so
    the caller instantiates the class it received.

    Fail-loud on unknown names via :class:`UnknownImplementationError`,
    matching every other loader in this module.
    """
    return cast(type[GradingSubstrate], _load(GRADING_SUBSTRATES_GROUP, name))


def available_runtime_backends() -> list[str]:
    """Sorted names registered in the ``tolokaforge.runtime_backends`` group."""
    return sorted(discover_entry_points(RUNTIME_BACKENDS_GROUP))


def available_trial_graders() -> list[str]:
    """Sorted names registered in the ``tolokaforge.trial_graders`` group."""
    return sorted(discover_entry_points(TRIAL_GRADERS_GROUP))


def available_conductors() -> list[str]:
    """Sorted names registered in the ``tolokaforge.conductors`` group."""
    return sorted(discover_entry_points(CONDUCTORS_GROUP))


def available_readiness_probes() -> list[str]:
    """Sorted kinds registered in the ``tolokaforge.service_readiness_probes`` group."""
    return sorted(discover_entry_points(SERVICE_READINESS_PROBES_GROUP))


def available_turn_policies() -> list[str]:
    """Sorted names registered in the ``tolokaforge.turn_policies`` group."""
    return sorted(discover_entry_points(TURN_POLICIES_GROUP))


def available_grading_substrates() -> list[str]:
    """Sorted names registered in the ``tolokaforge.grading_substrates`` group."""
    return sorted(discover_entry_points(GRADING_SUBSTRATES_GROUP))


def available_custom_check_executors() -> list[str]:
    """Sorted names registered in the ``tolokaforge.custom_check_executors`` group."""
    return sorted(discover_entry_points(CUSTOM_CHECK_EXECUTORS_GROUP))


def available_judge_model_providers() -> list[str]:
    """Sorted names registered in the ``tolokaforge.judge_model_providers`` group."""
    return sorted(discover_entry_points(JUDGE_MODEL_PROVIDERS_GROUP))
