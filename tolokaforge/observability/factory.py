"""Build the run's ``TrialObserver`` from ``observability.tracing`` (ADR-0047, packaging
amendment).

The engine owns the seam and the identity, not the receiver. This module resolves the run's
tracing identity (the external ``run_id`` a workflow hands in through the config or
``TOLOKAFORGE_TRACING_RUN_ID``, else the engine's run id, plus the ``run_tag`` namespace), asks
every installed **trial-observer plugin** for an observer, and composes what comes back. A plugin
is a callable registered under the ``tolokaforge.trial_observers`` entry-point group with the
signature ``build(tracing, identity, *, engine_run_id, output_dir) -> TrialObserver | None``;
``None`` means "nothing asks for me in this run". Receiver plugins ship independently; their
namespaced settings live in ``tracing.options`` and require no engine release to extend.

The identity is returned alongside so the conductor derives the same trace ids the offline
bundle uploader will, and is written to ``run_identity.json`` in the run directory for that
uploader to read whenever an observer is active. A plugin that cannot be loaded, or a config that
asks for an exporter no plugin provides, is a ``TracingConfigError`` at run start, with nothing to
tear down; nothing here raises into a trial later.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from tolokaforge.observability import ids
from tolokaforge.observability.observer import (
    CompositeTrialObserver,
    ExportReceipt,
    NullTrialObserver,
    TrialIdentity,
    TrialObserver,
)

if TYPE_CHECKING:
    from tolokaforge.core.models import ObservabilityConfig

TRIAL_OBSERVERS_GROUP = "tolokaforge.trial_observers"
"""The entry-point group a trial-observer plugin registers its ``build`` callable under."""
PLUGIN_API_VERSION = 4
"""The plugin contract this engine speaks: the ``build`` signature above, ``TracingConfig``,
the ``TrialObserver`` hooks and receipt shape of :mod:`tolokaforge.observability.observer`, and
the id contract of :mod:`tolokaforge.observability.ids`. A plugin compares it with the version
it was built for. Version 4 widens the id contract's kind set with the preview kinds, so a
plugin that emits them needs an engine whose ``ids`` knows them."""

RUN_IDENTITY_FILE = "run_identity.json"
TRACING_RECEIPT_FILE = "tracing_receipt.json"
TRACING_RUN_ID_ENV = "TOLOKAFORGE_TRACING_RUN_ID"
TRACING_RUN_TAG_ENV = "TOLOKAFORGE_TRACING_RUN_TAG"

_log = logging.getLogger(__name__)


class TracingConfigError(ValueError):
    """``observability.tracing`` cannot be honoured as written."""


@dataclass(frozen=True)
class RunIdentity:
    """The two id-contract components every trial of a run shares."""

    run_id: str
    run_tag: str = ids.DEFAULT_RUN_TAG

    def trial(self, task_id: str, trial_index: int, attempt_id: int) -> TrialIdentity:
        return TrialIdentity(
            run_id=self.run_id,
            task_id=task_id,
            trial_index=trial_index,
            attempt_id=attempt_id,
            run_tag=self.run_tag,
        )


def build_trial_observer(
    observability: ObservabilityConfig | None,
    *,
    engine_run_id: str,
    output_dir: Path | None = None,
) -> tuple[TrialObserver, RunIdentity]:
    """The observer for this run and the identity its trials trace under."""
    from tolokaforge.core.models import TracingConfig

    tracing = getattr(observability, "tracing", None)
    if not isinstance(tracing, TracingConfig):  # absent, or a caller's stub config
        tracing = None
    run_id = (tracing.run_id if tracing else None) or _env(TRACING_RUN_ID_ENV) or engine_run_id
    run_tag = (
        (tracing.run_tag if tracing and tracing.run_tag != ids.DEFAULT_RUN_TAG else None)
        or _env(TRACING_RUN_TAG_ENV)
        or (tracing.run_tag if tracing else None)
        or ids.DEFAULT_RUN_TAG
    )
    try:
        ids.check_component("run_id", run_id)
        ids.check_component("run_tag", run_tag)
    except ValueError as exc:
        raise TracingConfigError(str(exc)) from exc
    identity = RunIdentity(run_id=run_id, run_tag=run_tag)
    observers = build_plugin_observers(
        tracing, identity, engine_run_id=engine_run_id, output_dir=output_dir
    )
    if not observers:
        asked = None
        if tracing is not None and tracing.exporter != "none":
            asked = f"observability.tracing.exporter={tracing.exporter!r}"
        elif switches := tracing_switches():
            asked = " / ".join(f"{name}={value}" for name, value in switches)
        if asked:
            raise TracingConfigError(
                f"{asked} but no trial-observer plugin produced an observer (installed under "
                f"{TRIAL_OBSERVERS_GROUP!r}: {', '.join(installed_plugins()) or 'none'}). "
                "Install and configure an observer plugin supporting the requested exporter "
                "or tracing switch."
            )
        return NullTrialObserver(), identity
    observer: TrialObserver = (
        observers[0] if len(observers) == 1 else CompositeTrialObserver(tuple(observers))
    )
    if output_dir is not None:
        write_run_identity(Path(output_dir), identity, engine_version=engine_version())
    return observer, identity


TRACING_SWITCH_SUFFIX = "_TRACING_ENABLED"
"""A plugin's one-switch variable ends in this suffix (``<PLUGIN>_TRACING_ENABLED``): the engine
knows no receiver's name, but a switch that is on while no plugin produced an observer is a
misconfiguration it refuses at run start rather than a run silently without traces."""
_TRUE = frozenset({"1", "true", "yes", "on"})


def tracing_switches() -> list[tuple[str, str]]:
    """The ``*_TRACING_ENABLED`` variables that are on, sorted by name."""
    return sorted(
        (name, value.strip())
        for name, value in os.environ.items()
        if name.endswith(TRACING_SWITCH_SUFFIX) and value.strip().lower() in _TRUE
    )


def installed_plugins() -> list[str]:
    """The names registered under the trial-observer group, sorted."""
    return sorted(_discover())


def _discover() -> Mapping[str, Any]:
    """The trial-observer entry points; a duplicate name across two distributions is the same
    configuration error as every other tracing misconfiguration."""
    from tolokaforge.core.plugin_registry import RegistryError, discover_entry_points

    try:
        return discover_entry_points(TRIAL_OBSERVERS_GROUP)
    except RegistryError as exc:
        raise TracingConfigError(str(exc)) from exc


def build_plugin_observers(
    tracing: Any,
    identity: RunIdentity,
    *,
    engine_run_id: str,
    output_dir: Path | None,
) -> list[TrialObserver]:
    """Every installed plugin's answer, in name order, ``None`` answers dropped. A plugin that
    cannot be imported is a configuration error: an installed observer the run cannot use is
    a broken environment, not a run without tracing."""
    built: list[TrialObserver] = []
    for name, entry_point in sorted(_discover().items()):
        try:
            build = cast("Callable[..., TrialObserver | None]", entry_point.load())
        except Exception as exc:  # noqa: BLE001 - any import failure is the same finding
            raise TracingConfigError(
                f"trial-observer plugin {name!r} ({entry_point.value}) cannot be loaded: {exc}"
            ) from exc
        observer = build(tracing, identity, engine_run_id=engine_run_id, output_dir=output_dir)
        if observer is not None:
            built.append(observer)
    return built


def engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("tolokaforge")
    except Exception:  # noqa: BLE001 - version is informational
        return "unknown"


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


class RunIdentityDocument(BaseModel):
    """The persisted run identity consumed by bundle uploaders (ADR-0047)."""

    model_config = {"extra": "forbid", "frozen": True}

    run_id: str
    run_tag: str = ids.DEFAULT_RUN_TAG
    written_by: str = "tolokaforge"
    written_at: str
    engine_version: str | None = None


def write_run_identity(
    output_dir: Path, identity: RunIdentity, *, engine_version: str | None = None
) -> Path:
    """``run_identity.json`` next to ``trials/``: the identity the offline uploader must reuse,
    and the engine version it reads the native ``release`` field from."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / RUN_IDENTITY_FILE
    document = RunIdentityDocument(
        run_id=identity.run_id,
        run_tag=identity.run_tag,
        written_at=datetime.now(tz=timezone.utc).isoformat(),
        engine_version=engine_version or None,
    )
    path.write_text(document.model_dump_json(indent=1, exclude_none=True) + "\n", encoding="utf-8")
    return path


def write_tracing_receipt(output_dir: Path, receipt: ExportReceipt) -> Path:
    path = Path(output_dir) / TRACING_RECEIPT_FILE
    path.write_text(receipt.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path
