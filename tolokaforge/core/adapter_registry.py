"""Resolver for multi-harness runs: one adapter per ``harnesses`` entry.

A single-adapter run builds one :class:`~tolokaforge.adapters.base.BaseAdapter`
and never touches this module. A run declaring a ``harnesses:`` block builds one
adapter per entry and wraps them in a :class:`CompositeAdapter`, which routes
each trial to its entry's adapter via :meth:`BaseAdapter.for_entry` and answers
the handful of *run-level* decisions (docker-CLI need, docker-stack
requirements, adapter fingerprints) as an explicit union across entries.

Resolution is by ENTRY, not by a globally-unique task id: the conductor carries
the entry name on each trial and asks :meth:`CompositeAdapter.for_entry` for the
adapter, so two entries are free to use the same adapter type with different
parameters. Within this slice a task id must not appear under two entries — the
builder's overlap guard refuses that, pointing at the matrix-identity follow-up
(#1768) — which keeps today's ``trials/<task_id>/<idx>`` layout collision-free.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tolokaforge.adapters import BaseAdapter, DockerStackRequirements, get_adapter

if TYPE_CHECKING:
    from tolokaforge.adapters.base import AdapterEnvironment
    from tolokaforge.core.models import TaskConfig
    from tolokaforge.core.models.run_config import HarnessEntryConfig

__all__ = ["CompositeAdapter", "HarnessEntry", "build_composite_adapter"]


@dataclass
class HarnessEntry:
    """One resolved leg of a multi-harness run.

    Attributes:
        name: The entry's unique, filesystem-safe identifier (derived or
            authored at config parse time).
        config: The entry's parsed :class:`HarnessEntryConfig`.
        adapter: The built adapter instance that runs this entry's tasks.
        task_ids: The task ids this entry contributes to the run, in adapter
            enumeration order.
    """

    name: str
    config: HarnessEntryConfig
    adapter: BaseAdapter
    task_ids: list[str]


class CompositeAdapter(BaseAdapter):
    """A ``BaseAdapter`` that fans out to one sub-adapter per harness entry.

    Per-task calls are resolved by entry: the conductor calls
    :meth:`for_entry` and then drives the returned adapter with a task id. The
    composite itself cannot answer a bare per-task call — it raises, naming the
    resolution path — because the entry, not the task id, is the identity.

    Run-level decisions are answered as an explicit union across entries so the
    choice is never a silent single-pick:

    * :meth:`any_requires_docker_cli` — OR across entries,
    * :meth:`union_docker_stack_requirements` — merged union (fails loud on an
      irreconcilable conflict),
    * :meth:`fingerprints_by_type` — one fingerprint per distinct adapter type,
    * :meth:`agreed_trial_grader_name` — the grader name all entries agree on,
      or a raise naming the disagreement.

    Bare attributes that encode a single-adapter assumption
    (``trial_grader_name``, ``requires_docker_cli_in_runner``) and the
    single-adapter methods (``docker_stack_requirements``, ``fingerprint``)
    raise rather than return a default, so no caller silently reads one entry's
    answer for the whole run.
    """

    def __init__(self, entries: Sequence[HarnessEntry]):
        super().__init__({})
        if not entries:
            raise ValueError("CompositeAdapter requires at least one entry.")
        self._entries: dict[str, HarnessEntry] = {}
        for entry in entries:
            if entry.name in self._entries:
                raise ValueError(f"CompositeAdapter: duplicate entry name {entry.name!r}.")
            self._entries[entry.name] = entry

    @property
    def entries(self) -> dict[str, HarnessEntry]:
        """The resolved entries, keyed by name (insertion order preserved)."""
        return dict(self._entries)

    def for_entry(self, name: str) -> BaseAdapter:
        """The adapter that runs entry *name*; raises on an unknown name."""
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(
                f"No harness entry named {name!r}; known entries: {sorted(self._entries)}."
            )
        return entry.adapter

    # -- Run-level union accessors ----------------------------------------

    def any_requires_docker_cli(self) -> bool:
        """OR of each entry adapter's ``requires_docker_cli_in_runner`` flag.

        The runner image carries the Docker CLI when *any* entry's grading
        needs it — a union, never a single entry's answer.
        """
        return any(
            bool(type(entry.adapter).requires_docker_cli_in_runner)
            for entry in self._entries.values()
        )

    def union_docker_stack_requirements(self) -> DockerStackRequirements:
        """Merge every entry's ``docker_stack_requirements`` into one object.

        Booleans are OR'd; mounts and bind targets are de-duplicated. An
        irreconcilable conflict fails loud: two entries binding the same
        container path to different host paths, or declaring the same
        ``(compose_file, service)`` image build with different pinned image
        refs, cannot both be honoured, so the merge raises naming both entries.
        """
        merged = DockerStackRequirements()
        seen_mounts: set[Any] = set()
        # container_path -> (host_path, entry_name)
        seen_binds: dict[str, tuple[Any, str]] = {}
        # (compose_file, service) -> (expected_image_ref, entry_name)
        seen_builds: dict[tuple[Any, str], tuple[str, str]] = {}

        for name, entry in self._entries.items():
            req = entry.adapter.docker_stack_requirements()
            merged.mount_docker_socket |= req.mount_docker_socket
            merged.enable_dind |= req.enable_dind
            merged.needs_rag_service |= req.needs_rag_service

            for mount in req.task_pack_mounts:
                if mount not in seen_mounts:
                    seen_mounts.add(mount)
                    merged.task_pack_mounts.append(mount)

            for host_path, container_path in req.extra_runner_binds:
                existing = seen_binds.get(container_path)
                if existing is not None and existing[0] != host_path:
                    raise ValueError(
                        f"docker_stack_requirements conflict: entries "
                        f"{existing[1]!r} and {name!r} bind container path "
                        f"{container_path!r} to different host paths "
                        f"({existing[0]!r} vs {host_path!r}). Reconcile the "
                        "two entries' runner binds."
                    )
                if existing is None:
                    seen_binds[container_path] = (host_path, name)
                    merged.extra_runner_binds.append((host_path, container_path))

            for build in req.image_builds:
                key = (build.compose_file, build.service)
                existing_build = seen_builds.get(key)
                if existing_build is not None and existing_build[0] != build.expected_image_ref:
                    raise ValueError(
                        f"docker_stack_requirements conflict: entries "
                        f"{existing_build[1]!r} and {name!r} declare image build "
                        f"{build.service!r} in {build.compose_file} with different "
                        f"pinned refs ({existing_build[0]!r} vs "
                        f"{build.expected_image_ref!r})."
                    )
                if existing_build is None:
                    seen_builds[key] = (build.expected_image_ref, name)
                    merged.image_builds.append(build)

        return merged

    def fingerprints_by_type(self) -> dict[str, Any]:
        """One fingerprint per distinct adapter type across entries.

        Keyed by the entry's configured adapter type, matching the single-
        adapter ``adapter_fingerprints[<type>]`` shape. The first entry of each
        type that reports a non-``None`` fingerprint wins; types that report
        nothing are omitted.
        """
        fingerprints: dict[str, Any] = {}
        for entry in self._entries.values():
            adapter_type = entry.config.adapter
            if adapter_type in fingerprints:
                continue
            payload = entry.adapter.fingerprint()
            if payload is not None:
                fingerprints[adapter_type] = payload
        return fingerprints

    def agreed_trial_grader_name(self) -> str:
        """The trial-grader name every entry agrees on, else a raise.

        A mixed run has one grading transport. When no explicit
        ``config.grader.name`` overrides it, the entries must agree on their
        adapter-default ``trial_grader_name``; a disagreement is refused here,
        naming each entry and the name it wants, because the composite cannot
        silently pick one.
        """
        by_name: dict[str, list[str]] = {}
        for name, entry in self._entries.items():
            grader_name = type(entry.adapter).trial_grader_name
            by_name.setdefault(grader_name, []).append(name)
        if len(by_name) == 1:
            return next(iter(by_name))
        disagreement = ", ".join(
            f"{grader!r} ({', '.join(sorted(entries))})"
            for grader, entries in sorted(by_name.items())
        )
        raise ValueError(
            "Entries disagree on their default trial grader: "
            f"{disagreement}. Set an explicit `grader.name` on the run config "
            "to choose one transport for the whole run."
        )

    # -- Ambiguous single-adapter surfaces (fail loud) --------------------

    def _per_entry_only(self, method: str) -> RuntimeError:
        return RuntimeError(
            f"CompositeAdapter.{method}() cannot be called on the composite: a "
            "multi-harness run resolves the owning adapter per entry. Call "
            f"for_entry(<entry>).{method}(...) instead. Entries: "
            f"{sorted(self._entries)}."
        )

    @property
    def trial_grader_name(self) -> str:  # type: ignore[override]
        raise RuntimeError(
            "CompositeAdapter.trial_grader_name is ambiguous across entries "
            f"{sorted(self._entries)} — use agreed_trial_grader_name() (or an "
            "explicit grader.name)."
        )

    @property
    def requires_docker_cli_in_runner(self) -> bool:  # type: ignore[override]
        raise RuntimeError(
            "CompositeAdapter.requires_docker_cli_in_runner is ambiguous across "
            f"entries {sorted(self._entries)} — use any_requires_docker_cli()."
        )

    def docker_stack_requirements(self) -> DockerStackRequirements:
        raise RuntimeError(
            "CompositeAdapter.docker_stack_requirements() is ambiguous across "
            f"entries {sorted(self._entries)} — use "
            "union_docker_stack_requirements()."
        )

    def fingerprint(self) -> dict[str, Any] | None:
        raise RuntimeError(
            "CompositeAdapter.fingerprint() is ambiguous across entries "
            f"{sorted(self._entries)} — use fingerprints_by_type()."
        )

    # -- Per-task surface: resolve a specific entry first -----------------

    def get_task_ids(self) -> list[str]:
        raise self._per_entry_only("get_task_ids")

    def get_task(self, task_id: str) -> TaskConfig:
        raise self._per_entry_only("get_task")

    def get_task_dir(self, task_id: str) -> Any:
        raise self._per_entry_only("get_task_dir")

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        raise self._per_entry_only("create_environment")

    def get_tools(self, task_id: str) -> list[Any]:
        raise self._per_entry_only("get_tools")

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        raise self._per_entry_only("get_registry_tools")

    def get_system_prompt(self, task_id: str) -> str:
        raise self._per_entry_only("get_system_prompt")

    def get_grading_config(self, task_id: str) -> Any:
        raise self._per_entry_only("get_grading_config")

    def reset_environment(self, env: AdapterEnvironment) -> None:
        raise self._per_entry_only("reset_environment")

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        raise self._per_entry_only("compute_golden_hash")

    def to_task_description(self, task_id: str) -> Any:
        raise self._per_entry_only("to_task_description")


def build_composite_adapter(
    entries: Sequence[HarnessEntryConfig],
    params_for_entry: Callable[[HarnessEntryConfig], dict[str, Any]],
    validate_entry: Callable[[HarnessEntryConfig, BaseAdapter], None] | None = None,
) -> CompositeAdapter:
    """Build one adapter per entry, enumerate its tasks, and assemble the matrix.

    *params_for_entry* yields the adapter-construction params for an entry —
    the orchestrator supplies the same per-adapter param assembly a single
    run uses, scoped to the entry. Each entry's adapter is built via
    :func:`~tolokaforge.adapters.get_adapter`, its task ids enumerated (and
    filtered to the entry's explicit ``task_ids`` allow-list when one is set).

    *validate_entry*, when given, is called with each entry's config and freshly
    built adapter **after construction but before ``get_task_ids``** — the seam
    the orchestrator's per-entry execution-mode gate hangs on, so a bad entry is
    refused before any task enumeration or container work.

    Overlap guard: within this slice a task id may belong to only one entry.
    The guard refuses a config where one id appears under two entries, naming
    both entries and the id and pointing at the matrix-identity follow-up
    (#1768) — so the shared ``trials/<task_id>/<idx>`` layout stays
    collision-free until that migration lands.
    """
    harness_entries: list[HarnessEntry] = []
    owner_of: dict[str, str] = {}
    for config in entries:
        adapter = get_adapter(config.adapter, params_for_entry(config))
        if validate_entry is not None:
            validate_entry(config, adapter)
        task_ids = list(adapter.get_task_ids())
        if config.task_ids:
            allow = set(config.task_ids)
            task_ids = [task_id for task_id in task_ids if task_id in allow]
        for task_id in task_ids:
            prior = owner_of.get(task_id)
            if prior is not None:
                raise ValueError(
                    f"Task id {task_id!r} appears under two harness entries "
                    f"({prior!r} and {config.name!r}). This slice resolves by "
                    "entry and keeps the shared trials/<task_id>/<idx> output "
                    "layout, so task ids must be distinct across entries. "
                    "Running the same task under multiple harnesses (a true "
                    "matrix) is the matrix-identity follow-up (#1768)."
                )
            owner_of[task_id] = config.name
        harness_entries.append(
            HarnessEntry(
                name=config.name,  # type: ignore[arg-type]  # filled at parse time
                config=config,
                adapter=adapter,
                task_ids=task_ids,
            )
        )
    return CompositeAdapter(harness_entries)
