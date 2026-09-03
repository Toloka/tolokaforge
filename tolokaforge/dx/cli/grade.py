"""``tolokaforge grade`` / ``tolokaforge grade-run`` — offline regrade verbs.

``grade <bundle-uri>`` resolves a ``bundle://<store>/<digest>`` URI
through a registered
:class:`~tolokaforge.core.grading.bundle_store.BundleStore`, wraps the
materialised bundle in a
:class:`~tolokaforge.core.grading.substrate.SnapshotGradingSubstrate`,
dispatches through a registered ``tolokaforge.grader_kinds`` implementation,
and writes ``grade.json`` under ``--out``. ``grade-run <run-dir>`` walks
a completed run's trial subtree, filters trials whose
:attr:`Trajectory.snapshot_status.outcome` is
:attr:`SnapshotOutcome.STORED`, and dispatches each through the same
single-trial helper — the whole verb sits on the shared
:func:`_regrade_bundle` seam.

Every dependency is already shipped: the store registry (#1355), the
reader / view (#1354), the snapshot substrate (#1353), the kind
registry (#1358), and the Stage 1 writer that persists
``snapshot_status`` on ``trajectory.yaml`` (#1359).

**Byte-parity commitment.** Live-grade vs CLI-regrade byte-parity holds
only for kinds whose substrate reads all succeed offline against
``SnapshotGradingSubstrate``. Bundle format v1.0 does not carry a
test-suite hook: ``test_execution`` refuses actionably via
:class:`SubstrateUnreachableError`. ``composite`` needs pre-computed
component scores (see ``docs/GRADER_SERVICE.md`` § Sub-component
plug-in seams) and today requires ``--grader-config components:{...}``.

Exit codes:

- ``0`` — every dispatched trial produced a grade (or, for ``grade``,
  the single dispatch succeeded). Skips are non-error.
- ``1`` — kind refused, substrate unreachable, kind returned no verdict,
  grading failed, or (for ``grade``) ``--out`` was not empty. No
  ``grade.json`` is written for the offending trial(s).
- ``2`` — bad argument at CLI-parse time (unknown kind, malformed URI,
  store-name mismatch on the single-trial verb, malformed
  ``--store-config`` YAML).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import TypeAdapter, ValidationError
from rich.markup import escape

from tolokaforge.core.grading.bundle import (
    BundleError,
    GradeBundleView,
    load_grade_bundle,
)
from tolokaforge.core.grading.bundle_store import (
    BundleStore,
    BundleStoreError,
    InvalidBundleURIError,
    LocalDiskBundleStore,
    parse_bundle_uri,
)
from tolokaforge.core.grading.kinds import GraderKindRefusedError
from tolokaforge.core.grading.substrate import (
    SnapshotGradingSubstrate,
    SubstrateUnreachableError,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Trajectory
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.run_config import BundleStoreBackend
from tolokaforge.core.models.trajectory import SnapshotOutcome, SnapshotStatus
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_grader_kinds,
    load_bundle_store,
)
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.dx._display import console
from tolokaforge.runner.models import RunnerGradingConfig

_GRADE_FILENAME = "grade.json"
_LOGGER_NAME = "tolokaforge.dx.grade"


def _load_kind_config(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise click.BadParameter(
            f"--grader-config at {path} must be a YAML mapping; parsed as {type(raw).__name__}",
        )
    return raw


def _load_store(store_config_path: Path | None) -> BundleStore:
    """Construct a :class:`BundleStore` from ``--store-config``.

    Absent → :class:`LocalDiskBundleStore` under ``Path.cwd()``. Present →
    parses the YAML through :data:`BundleStoreBackend`'s discriminated
    union (``extra="forbid"`` catches a mis-tagged block loudly) and
    resolves the ``type`` field through the ``tolokaforge.bundle_stores``
    registry.
    """
    if store_config_path is None:
        return LocalDiskBundleStore(root_dir=Path.cwd())
    try:
        raw = yaml.safe_load(store_config_path.read_text(encoding="utf-8"))
        backend = TypeAdapter(BundleStoreBackend).validate_python(raw)
    except ValidationError as exc:
        raise click.BadParameter(
            f"--store-config at {store_config_path} did not parse as a BundleStoreBackend: {exc}"
        ) from exc
    try:
        store_cls = load_bundle_store(backend.type)
    except UnknownImplementationError as exc:
        raise click.BadParameter(str(exc)) from exc
    kwargs = backend.model_dump(exclude={"type"}, exclude_none=True)
    return store_cls(**kwargs)


def _resolve_kind_cls(kind_name: str) -> type:
    from tolokaforge.core.plugin_registry import load_grader_kind

    try:
        return load_grader_kind(kind_name)
    except UnknownImplementationError as exc:
        available = ", ".join(available_grader_kinds()) or "<none>"
        raise click.BadParameter(
            f"grader-kind {kind_name!r} is not registered under "
            f"tolokaforge.grader_kinds. Registered: {available}."
        ) from exc


def _guard_out_dir(out_dir: Path) -> None:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise click.ClickException(
            f"--out at {out_dir} is not empty; refusing to overwrite existing "
            "artifacts. Point --out at a fresh directory."
        )
    out_dir.mkdir(parents=True, exist_ok=True)


def _read_grading_config(bundle_view: GradeBundleView) -> RunnerGradingConfig:
    return RunnerGradingConfig.model_validate(
        json.loads(bundle_view.open_part("grading_config.json"))
    )


def _regrade_bundle(
    bundle_uri: str,
    *,
    kind_name: str,
    kind_config: Mapping[str, Any] | None,
    store: BundleStore,
    out_dir: Path,
) -> Grade:
    """Resolve → materialise → substrate → dispatch → write.

    Returns the produced :class:`Grade` (already written under
    ``out_dir/grade.json``). Never prints — the caller (single-trial
    ``grade`` verb or batch ``grade-run`` verb) owns display. Raises
    :class:`click.BadParameter` for URI / kind / store-name arg defects
    and :class:`click.ClickException` for a refusal / unreachable
    substrate / failed grading / no-verdict.
    """
    try:
        store_name, _digest = parse_bundle_uri(bundle_uri)
    except InvalidBundleURIError as exc:
        raise click.BadParameter(f"not a bundle URI: {exc}") from exc
    if store_name != store.name:
        raise click.BadParameter(
            f"URI names store {store_name!r} but --store-config selected {store.name!r}"
        )

    kind_cls = _resolve_kind_cls(kind_name)

    with tempfile.TemporaryDirectory(prefix="tolokaforge-grade-") as scratch:
        bundle_dir = Path(scratch) / "bundle"
        try:
            store.get(bundle_uri, bundle_dir)
        except InvalidBundleURIError as exc:
            raise click.BadParameter(str(exc)) from exc
        except BundleError as exc:
            raise click.ClickException(f"bundle load failed: {exc}") from exc
        except BundleStoreError as exc:
            raise click.ClickException(f"bundle load failed: {exc}") from exc
        try:
            bundle_view = load_grade_bundle(bundle_dir)
        except BundleError as exc:
            raise click.ClickException(f"bundle load failed: {exc}") from exc
        grading_config = _read_grading_config(bundle_view)
        substrate = SnapshotGradingSubstrate(bundle_view)
        try:
            try:
                grade = kind_cls().evaluate(
                    substrate=substrate,
                    task_config=grading_config,
                    kind_config=kind_config,
                    trial_id=bundle_view.manifest.trial_id,
                    agent_tools={},
                    logger=get_logger(_LOGGER_NAME),
                )
            except GraderKindRefusedError as exc:
                raise click.ClickException(f"kind {kind_name} refused: {exc.reason}") from exc
            except SubstrateUnreachableError as exc:
                raise click.ClickException(f"substrate unreachable: {exc}") from exc
            except GradingFailedError as exc:
                raise click.ClickException(f"grading failed: {exc}") from exc
        finally:
            substrate.close()

    if grade is None:
        raise click.ClickException(f"kind {kind_name} returned no verdict")

    target = out_dir / _GRADE_FILENAME
    target.write_text(grade.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    return grade


@click.command(name="grade")
@click.argument("bundle_uri")
@click.option(
    "--grader-kind",
    "-k",
    "grader_kind",
    required=True,
    metavar="KIND",
    help=(
        "Grader-kind name registered under tolokaforge.grader_kinds. "
        "Query the registered set with tolokaforge.core.plugin_registry."
        "available_grader_kinds()."
    ),
)
@click.option(
    "--grader-config",
    "grader_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Optional YAML file whose contents are passed to evaluate(kind_config=...). "
        "The kind validates the shape internally."
    ),
)
@click.option(
    "--store-config",
    "store_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Optional YAML file describing the BundleStore. Discriminated on 'type' "
        "(local_disk | s3). Absent -> LocalDiskBundleStore(root_dir=cwd())."
    ),
)
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Destination directory for grade.json. Refuses a non-empty target.",
)
def grade(
    bundle_uri: str,
    grader_kind: str,
    grader_config: Path | None,
    store_config: Path | None,
    out: Path,
) -> None:
    """Regrade a bundle offline against SnapshotGradingSubstrate.

    Reads the bundle at BUNDLE_URI via the store described by
    --store-config, dispatches the kind named by --grader-kind, and writes
    grade.json under --out.
    """
    kind_config = _load_kind_config(grader_config)
    store = _load_store(store_config)
    _guard_out_dir(out)
    try:
        try:
            _regrade_bundle(
                bundle_uri,
                kind_name=grader_kind,
                kind_config=kind_config,
                store=store,
                out_dir=out,
            )
        except click.ClickException as exc:
            console.print(f"[error]{escape(exc.message)}[/error]")
            raise
    finally:
        store.close()

    console.print(
        f"[success]regraded[/success] {escape(bundle_uri)} -> {escape(str(out / _GRADE_FILENAME))}"
    )


# ---------------------------------------------------------------------------
# grade-run — batch over a completed run's trial subtree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DiscoveredTrial:
    """One trajectory-yaml hit under ``<run-dir>/trials/<task>/<idx>/``."""

    task_id: str
    trial_idx: str
    trajectory_path: Path

    @property
    def label(self) -> str:
        return f"{self.task_id}/{self.trial_idx}"


def _discover_trials(run_dir: Path) -> list[_DiscoveredTrial]:
    trials_root = run_dir / "trials"
    if not trials_root.is_dir():
        raise click.BadParameter(
            f"{run_dir} does not contain a 'trials/' subdirectory; not a completed run",
        )
    hits = sorted(trials_root.glob("*/*/trajectory.yaml"))
    return [
        _DiscoveredTrial(
            task_id=path.parent.parent.name,
            trial_idx=path.parent.name,
            trajectory_path=path,
        )
        for path in hits
    ]


def _classify_skip_reason(status: SnapshotStatus | None) -> str | None:
    """``None`` when the trial is dispatchable; otherwise the skip reason."""
    if status is None:
        return "no snapshot_status recorded (run predates snapshot mode?)"
    if status.outcome is SnapshotOutcome.UNGRADED:
        return "trial ended before grading"
    if status.outcome is SnapshotOutcome.OVERSIZE:
        return f"bundle oversize ({status.bundle_size_bytes} > {status.cap_bytes})"
    if status.outcome is SnapshotOutcome.PRODUCE_FAILED:
        return status.reason or "producer failed"
    return None


@click.command(name="grade-run")
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--with-kind",
    "with_kind",
    required=True,
    metavar="KIND",
    help=(
        "Grader-kind name registered under tolokaforge.grader_kinds. Same "
        "validation as `tolokaforge grade --grader-kind`; different flag "
        "name reads more naturally in a batch verb."
    ),
)
@click.option(
    "--grader-config",
    "grader_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Optional YAML file passed to evaluate(kind_config=...) for every "
        "dispatched trial. The kind validates the shape internally."
    ),
)
@click.option(
    "--store-config",
    "store_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Optional YAML file describing the BundleStore. Same discriminated "
        "union as `tolokaforge grade`. Absent -> LocalDiskBundleStore(root_dir=cwd())."
    ),
)
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Root directory for per-trial grade.json files. Each dispatched "
        "trial lands at <out>/<task>/<idx>/grade.json; refuses a non-empty --out."
    ),
)
def grade_run(
    run_dir: Path,
    with_kind: str,
    grader_config: Path | None,
    store_config: Path | None,
    out: Path,
) -> None:
    """Regrade every stored-bundle trial under a completed run.

    Walks RUN_DIR/trials/<task>/<idx>/trajectory.yaml, filters to trials
    whose snapshot_status.outcome == stored, and dispatches each through
    the same single-trial pipeline as `tolokaforge grade`. Exits 0 iff no
    dispatch failed; skips are non-error.
    """
    kind_config = _load_kind_config(grader_config)
    store = _load_store(store_config)
    _guard_out_dir(out)

    discovered = _discover_trials(run_dir)

    regraded = 0
    skipped = 0
    failed = 0
    try:
        for trial in discovered:
            trajectory = Trajectory.model_validate(
                yaml.safe_load(trial.trajectory_path.read_text(encoding="utf-8"))
            )
            skip_reason = _classify_skip_reason(trajectory.snapshot_status)
            if skip_reason is not None:
                console.print(f"[warn]skip[/warn] {escape(trial.label)} - {escape(skip_reason)}")
                skipped += 1
                continue

            assert trajectory.snapshot_status is not None
            uri = trajectory.snapshot_status.uri
            if uri is None:
                console.print(
                    f"[warn]skip[/warn] {escape(trial.label)} - stored outcome carries no uri"
                )
                skipped += 1
                continue

            trial_out = out / trial.task_id / trial.trial_idx
            trial_out.mkdir(parents=True, exist_ok=True)
            try:
                _regrade_bundle(
                    uri,
                    kind_name=with_kind,
                    kind_config=kind_config,
                    store=store,
                    out_dir=trial_out,
                )
            except click.ClickException as exc:
                console.print(
                    f"[error]failed[/error] {escape(trial.label)} - {escape(exc.message)}"
                )
                failed += 1
                continue
            console.print(
                f"[success]regraded[/success] {escape(trial.label)} -> "
                f"{escape(str(trial_out / _GRADE_FILENAME))}"
            )
            regraded += 1
    finally:
        store.close()

    console.print(
        f"Regraded: discovered {len(discovered)}, regraded {regraded}, "
        f"skipped {skipped}, failed {failed}"
    )
    if failed:
        raise click.ClickException(f"{failed} trial(s) failed to regrade")
