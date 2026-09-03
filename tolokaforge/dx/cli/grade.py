"""``tolokaforge grade`` — regrade a stored bundle offline.

Resolves a ``bundle://<store>/<digest>`` URI through a registered
:class:`~tolokaforge.core.grading.bundle_store.BundleStore`, wraps the
materialised bundle in a
:class:`~tolokaforge.core.grading.substrate.SnapshotGradingSubstrate`,
dispatches through a registered ``tolokaforge.grader_kinds`` implementation,
and writes ``grade.json`` under ``--out``. Every dependency is already
shipped: the store registry (#1355), the reader / view (#1354), the
snapshot substrate (#1353), and the kind registry (#1358).

**Byte-parity commitment.** Live-grade vs CLI-regrade byte-parity holds
only for kinds whose substrate reads all succeed offline against
``SnapshotGradingSubstrate``. Bundle format v1.0 does not carry a
test-suite hook: ``test_execution`` refuses actionably via
:class:`SubstrateUnreachableError`. ``composite`` needs pre-computed
component scores (see ``docs/GRADER_SERVICE.md`` § Sub-component
plug-in seams) and today requires ``--grader-config components:{...}``.

Exit codes:

- ``0`` — grade written.
- ``1`` — kind refused, substrate unreachable, kind returned no verdict,
  or grading itself failed. ``grade.json`` is not written.
- ``2`` — bad argument (unknown kind, malformed URI, store-name mismatch,
  malformed ``--store-config`` YAML).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
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
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.run_config import BundleStoreBackend
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
    ``out_dir/grade.json``). Raises :class:`click.BadParameter` for URI /
    kind / store-name defects, :class:`click.ClickException` for a
    refusal / unreachable substrate / failed grading / no-verdict.
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
                console.print(
                    f"[error]Kind {kind_name} refused this bundle:[/error] {escape(exc.reason)}"
                )
                raise click.ClickException(f"kind {kind_name} refused: {exc.reason}") from exc
            except SubstrateUnreachableError as exc:
                console.print(f"[error]Substrate unreachable:[/error] {escape(str(exc))}")
                raise click.ClickException(f"substrate unreachable: {exc}") from exc
            except GradingFailedError as exc:
                console.print(f"[error]Grading failed:[/error] {escape(str(exc))}")
                raise click.ClickException(f"grading failed: {exc}") from exc
        finally:
            substrate.close()

    if grade is None:
        console.print(f"[warn]Kind {kind_name} returned no verdict for {escape(bundle_uri)}[/warn]")
        raise click.ClickException(f"kind {kind_name} returned no verdict")

    target = out_dir / _GRADE_FILENAME
    target.write_text(grade.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    console.print(f"[success]regraded[/success] {escape(bundle_uri)} -> {escape(str(target))}")
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
        _regrade_bundle(
            bundle_uri,
            kind_name=grader_kind,
            kind_config=kind_config,
            store=store,
            out_dir=out,
        )
    finally:
        store.close()
