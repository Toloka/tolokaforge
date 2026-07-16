"""Backend-capability registry + admission gate.

A task pack declares the capabilities its run needs via
``compute.capabilities``; the selected runtime backend advertises the
capabilities it can honour via :attr:`RuntimeBackend.advertised_capabilities`.
Admission (:func:`check_admission`) refuses to start the run when the
request is not a subset of the advertisement, naming the offending
capability names so the operator sees the exact gap.

Vocabulary rule: every requested name must appear in
:data:`CAPABILITY_REGISTRY`. Unknown names fail loud — the registry is
the closed set of names the engine understands. Local-docker's advertised
set is a subset of that vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilitySpec:
    """Declarative entry for one capability name.

    ``params_schema`` is a lightweight description of the parameter
    payload accepted by ``{"name": {params}}`` style entries. ``None``
    marks the capability as parameterless (``"name"`` bare-string
    entries are the canonical form).
    """

    name: str
    description: str
    params_schema: dict[str, Any] | None = None


_LOCAL_DOCKER_BASELINE: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        name="per_trial_stack",
        description="Backend materialises a fresh substrate per trial.",
    ),
    CapabilitySpec(
        name="shared_stack",
        description="Backend materialises one substrate for the whole run.",
    ),
    CapabilitySpec(
        name="reset_recipes:sql_dump",
        description="Backend can restore a Postgres/SQL dump into a labelled service between trials.",
    ),
    CapabilitySpec(
        name="reset_recipes:filesystem_dir",
        description="Backend can copy a directory tree into a labelled service's workspace between trials.",
    ),
    CapabilitySpec(
        name="reset_recipes:redis_dump",
        description="Backend can restore an RDB snapshot into a labelled Redis service between trials.",
    ),
    CapabilitySpec(
        name="reset_recipes:bare",
        description="Backend consumes seed files without an automatic overlay; task compose owns the load.",
    ),
    CapabilitySpec(
        name="network_isolation:no_internet",
        description="Backend attaches task application services to an internal network with no public egress; the runner keeps egress for grading.",
    ),
)


CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
    spec.name: spec for spec in _LOCAL_DOCKER_BASELINE
}
"""Closed vocabulary of capability names the engine understands. New
substrates (Kubernetes, Modal, ...) add their entries at import time,
same shape."""


LOCAL_DOCKER_ADVERTISED: frozenset[str] = frozenset(spec.name for spec in _LOCAL_DOCKER_BASELINE)
"""Baseline capability set advertised by the local-docker backends —
:class:`PerTrialRuntimeBackend` and :class:`SharedStackRuntimeBackend`.
Each backend refines this to the subset it actually honours."""


def check_admission(requested: list[Any], advertised: frozenset[str]) -> None:
    """Raise ``RuntimeError`` unless every requested capability appears
    in both the registry and the advertised set.

    ``requested`` is the raw ``compute.capabilities`` list — a mix of
    bare-string names and single-key ``{"name": {params}}`` dicts.
    Requested names are extracted here so the caller doesn't repeat the
    coercion. Two distinct errors surface:

    * **Unknown name** — the request names a capability absent from
      :data:`CAPABILITY_REGISTRY`. The registry, not the backend, is the
      authority on which names are legal.
    * **Missing advertisement** — the name is legal but the selected
      backend does not honour it.
    """
    requested_names = _extract_names(requested)
    unknown = sorted(name for name in requested_names if name not in CAPABILITY_REGISTRY)
    if unknown:
        raise RuntimeError(
            f"Unknown compute.capabilities entries: {unknown!r}. "
            f"Registered names: {sorted(CAPABILITY_REGISTRY)!r}."
        )
    missing = sorted(name for name in requested_names if name not in advertised)
    if missing:
        raise RuntimeError(
            f"Selected runtime backend does not advertise required "
            f"capabilities: {missing!r}. Backend advertises: "
            f"{sorted(advertised)!r}."
        )


def _extract_names(entries: list[Any]) -> list[str]:
    """Coerce raw capability entries into their names.

    ``ComputeConfig._validate_capability_entries`` already validated the
    shape at parse time, so this helper is a pure extraction — no
    fallback branches to hide malformed input.
    """
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and len(entry) == 1:
            (name,) = entry.keys()
            names.append(name)
        else:
            raise ValueError(
                f"compute.capabilities entry has unexpected shape: {entry!r}. "
                "Bare-string names or single-key dicts only."
            )
    return names


__all__ = [
    "CAPABILITY_REGISTRY",
    "CapabilitySpec",
    "LOCAL_DOCKER_ADVERTISED",
    "check_admission",
]
