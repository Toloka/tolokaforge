"""Grade-bundle producer helper — orchestrator-side bundle materialisation.

Composes substrate reads plus caller-supplied trajectory + task-description
inputs into a v1.0 grade bundle via
:func:`~tolokaforge.core.grading.bundle.serialize_grade_bundle`. The
orchestrator's trial-end producer seam is the sole caller; the runtime
backend delegates to this module inside its
:meth:`~tolokaforge.core.runtime.RuntimeBackend.build_grade_bundle`
implementation.

Purity: stdlib plus :mod:`tolokaforge.core.grading.bundle` only. All
inputs are typed structurally (``Any``) — a
:class:`~tolokaforge.core.grading.substrate.GradingSubstrate`
implementation, a :class:`~tolokaforge.core.models.trajectory.Trajectory`,
and a :class:`~tolokaforge.runner.models.TaskDescription`. Callers hold
the concrete types; this module reads only the members it needs.
A concrete ``GradingSubstrate`` runtime import would transitively reach
``runner.models`` / ``grader.wire_snapshot`` via
:mod:`tolokaforge.core.grading.substrate`'s TYPE_CHECKING imports of
``judge`` / ``kb_search``, tripping the ``bundle-producer-purity``
contract (``allow_indirect_imports = false``). Structural typing keeps
the module decoupled and the contract green.

No reach into ``tolokaforge.runner``, ``tolokaforge.grader``, or
``tolokaforge.core.grading.substrate_live`` — locked by the
``bundle-producer-purity`` contract in ``.importlinter``
(``allow_indirect_imports = false``).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.bundle import (
    GradeBundleManifest,
    serialize_grade_bundle,
)

__all__ = [
    "bundle_dir_size_bytes",
    "serialize_bundle_from_substrate",
]


def serialize_bundle_from_substrate(
    *,
    substrate: Any,
    trial_id: str,
    out_dir: Path,
    trajectory: Any,
    task_description: Any,
) -> GradeBundleManifest:
    """Compose substrate reads + trajectory + task-description into a bundle.

    ``substrate`` must satisfy the
    :class:`~tolokaforge.core.grading.substrate.GradingSubstrate` Protocol
    structurally. Reads ``initial_state`` / ``final_state`` /
    ``final_state_stable`` and ``filesystem_root`` from it. Decodes
    ``task_description.tool_artifacts`` (base64 ``dict[str, str]``) to
    ``dict[str, bytes]``. Serialises the trajectory via
    ``trajectory.model_dump(mode="json")``. Emits ``task_description.grading``
    as ``grading_config`` (``{}`` when absent).

    ``kb`` is not populated — bundle format v1.0 carries raw KB bytes,
    which :class:`~tolokaforge.core.grading.substrate.SnapshotGradingSubstrate`
    does not consume.

    Raises :class:`ValueError` when ``substrate.filesystem_root()`` returns
    ``None`` — bundle format v1.0 requires ``filesystem.tar``, and every
    live trial has a workspace root, so a ``None`` here indicates a caller
    bug rather than an empty workspace.
    """
    filesystem_root = substrate.filesystem_root()
    if filesystem_root is None:
        raise ValueError(
            f"Cannot build grade bundle for {trial_id}: substrate has no "
            "filesystem root; snapshot mode requires an agent-visible workspace."
        )
    tool_artifacts_b64 = task_description.tool_artifacts or {}
    tool_artifacts: dict[str, bytes] = {
        rel: base64.b64decode(payload) for rel, payload in tool_artifacts_b64.items()
    }
    grading = task_description.grading
    grading_config = grading.model_dump(mode="json") if grading is not None else {}
    return serialize_grade_bundle(
        out_dir,
        trial_id=trial_id,
        initial_state=substrate.initial_state(),
        final_state=substrate.final_state(),
        final_state_stable=substrate.final_state_stable(),
        filesystem_root=filesystem_root,
        checks=tool_artifacts or None,
        kb=None,
        trajectory=trajectory.model_dump(mode="json"),
        grading_config=grading_config,
    )


def bundle_dir_size_bytes(bundle_dir: Path) -> int:
    """Total on-disk size of every file under ``bundle_dir`` in bytes.

    One ``stat`` per file — the bundle carries a bounded number of parts
    (~10 files plus optional ``checks/`` and ``kb/`` subtrees). The
    on-disk walk is authoritative: a producer bug that writes extra
    un-manifested files still counts toward the cap decision.
    """
    return sum(p.stat().st_size for p in bundle_dir.rglob("*") if p.is_file())
