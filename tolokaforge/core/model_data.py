"""Public engine seam for bundled model-data resources.

The module exposes two related surfaces:

* Three path accessors — :func:`bundled_pricing_path`,
  :func:`bundled_presets_path`, :func:`bundled_providers_path` — that
  return the on-disk location of the engine's bundled model-data files
  (``pricing.json``, ``model_presets.yaml``, ``providers.yaml``).
  Consumers parse and validate the file's contents themselves; the
  accessors only guarantee the file exists.
* The fingerprint schema types (:class:`ModelsFingerprint`,
  :data:`MODELS_PACKAGE_VERSION`, :data:`MODELS_MINIMUM_ENGINE_VERSION`,
  :data:`MODELS_FINGERPRINT_API_VERSION`) plus
  :func:`decode_models_fingerprint`, the read-side decoder used by
  :mod:`tolokaforge.core.engine_run_state`.

The module has **no first-party imports** — it is safe to import from
runner-subset code. :func:`load_policy_registrations` inlines its
``importlib.metadata`` enumeration (rather than routing through
:mod:`tolokaforge.core.plugin_registry`) so this module's import graph stays
light and free of the presets-side circular reach the plugin registry has via
:mod:`tolokaforge.core.loop`. The fingerprint compute path (which needs the
resolved preset table, pricing dict, and certificate registry) lives in the
orchestrator-only sibling :mod:`tolokaforge.core.model_data_fingerprint`.

``_DATA_ROOT`` is the internal seam pointing at the bundled data
directory. Tests monkey-patch this constant to redirect the accessors at
a scratch tree.

See ADR-0030 § "The one seam" and § "Fingerprinting for auditability"
for the wheel-split context. While the model data still ships in the
engine wheel, :data:`MODELS_PACKAGE_VERSION` is the literal ``"in-tree"``
sentinel; when the ``tolokaforge-models`` wheel-split lands the three
module constants will be sourced from that wheel's ``__init__``.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

__all__ = [
    "MODELS_FINGERPRINT_API_VERSION",
    "MODELS_MINIMUM_ENGINE_VERSION",
    "MODELS_PACKAGE_VERSION",
    "ModelsFingerprint",
    "bundled_presets_path",
    "bundled_pricing_path",
    "bundled_providers_path",
    "decode_models_fingerprint",
    "load_policy_registrations",
]

#: Entry-point group each installed models wheel declares its per-model policy
#: subclasses under. See :func:`load_policy_registrations`.
POLICIES_GROUP: Final[str] = "tolokaforge.policies"


_DATA_ROOT: Final[Path] = Path(str(importlib.resources.files("tolokaforge.core") / "data"))
"""Internal seam pointing at the directory holding the bundled model-data
files. The three accessors resolve their targets under this root; tests
monkey-patch this constant to redirect them at a scratch tree. The
future ``tolokaforge-models`` wheel-split flips this single line to
``importlib.resources.files("tolokaforge_models") / "data"`` — accessor
bodies stay the same and consumers see no change."""


def bundled_pricing_path() -> Path:
    """Return the on-disk path of the bundled ``pricing.json`` table.

    Raises :class:`FileNotFoundError` when the file is absent — a
    corrupted install shape the caller must surface, not swallow. The
    accessor does **not** open, parse, or validate the file; the
    consumer (``tolokaforge.core.pricing._load_pricing``) is responsible
    for rejecting empty / malformed content with its own loud failure.

    Public API. Stable within v0.17.x — downstream code that reads the
    engine's bundled pricing table should use this accessor instead of
    reaching into ``tolokaforge/core/data/`` directly.
    """
    path = _DATA_ROOT / "pricing.json"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled pricing table not found at {path} — corrupted install?")
    return path


def bundled_presets_path() -> Path:
    """Return the on-disk path of the bundled ``model_presets.yaml`` table.

    Raises :class:`FileNotFoundError` when the file is absent — a
    corrupted install shape the caller must surface, not swallow. The
    accessor does **not** open, parse, or validate the file; the
    consumer (``tolokaforge.core.llm.presets._load_bundled_presets``) is
    responsible for rejecting empty / malformed content with its own
    loud failure.

    Public API. Stable within v0.17.x — downstream code that reads the
    engine's bundled preset table should use this accessor instead of
    reaching into ``tolokaforge/core/data/`` directly.
    """
    path = _DATA_ROOT / "model_presets.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled preset table not found at {path} — corrupted install?")
    return path


def bundled_providers_path() -> Path:
    """Return the on-disk path of the bundled ``providers.yaml`` table.

    Raises :class:`FileNotFoundError` when the file is absent — a
    corrupted install shape the caller must surface, not swallow. The
    accessor does **not** open, parse, or validate the file; the
    consumer (``tolokaforge.core.llm.providers._load_bundled_providers``)
    is responsible for rejecting empty / malformed content with its own
    loud failure.

    Public API. Stable within v0.17.x — downstream code that reads the
    engine's bundled provider table should use this accessor instead of
    reaching into ``tolokaforge/core/data/`` directly.
    """
    path = _DATA_ROOT / "providers.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled provider table not found at {path} — corrupted install?")
    return path


#: Sentinel used while model data ships in the engine wheel; a real
#: PEP 440 version string replaces it when the wheel-split lands.
MODELS_PACKAGE_VERSION: Final[str] = "in-tree"

#: PEP 440 specifier naming the engine floor the current bundled model
#: data (tracking #931's widened :class:`ModelCertificate`) is compatible
#: with.
MODELS_MINIMUM_ENGINE_VERSION: Final[str] = ">=0.17,<0.18"

#: Integer version of the fingerprint payload contract; bumped whenever
#: :func:`tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
#: changes the shape of the hashed payload in a way readers must know
#: about.
MODELS_FINGERPRINT_API_VERSION: Final[int] = 1


class ModelsFingerprint(BaseModel):
    """Resolved model-data snapshot recorded on ``engine_run_state.json``.

    Fields
    ------
    package_version:
        The ``tolokaforge-models`` PEP 440 version, or the literal
        ``"in-tree"`` sentinel while the data still ships in the engine
        wheel.
    content_sha256:
        Lowercase hex sha256 over the canonicalised
        ``{presets, pricing, certificates}`` triple. Same inputs →
        byte-identical digest; any overlay tweak → different digest.
    api_version:
        Contract version of the hashed payload — see
        :data:`MODELS_FINGERPRINT_API_VERSION`.
    minimum_engine_version:
        PEP 440 specifier the model-data snapshot requires the engine to
        satisfy — see :data:`MODELS_MINIMUM_ENGINE_VERSION`.
    """

    model_config = ConfigDict(extra="forbid")

    package_version: str
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    api_version: Literal[1]
    minimum_engine_version: str

    @field_validator("minimum_engine_version")
    @classmethod
    def _validate_minimum_engine_version(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"not a valid PEP 440 specifier: {value!r}") from exc
        return value


def load_policy_registrations() -> dict[str, dict[str, type]]:
    """Discover per-model policy classes shipped by installed models wheels.

    Each entry point in the :data:`POLICIES_GROUP` group declares its slot and
    policy name via a dotted key ``"<slot>.<name>"`` (for example
    ``"schema_sanitizer.gemini_recursive"``); the entry point's value resolves
    to the class object. :mod:`tolokaforge.core.llm.presets` calls this at
    module import time and merges the returned mapping onto
    ``_POLICY_REGISTRIES`` after the engine's built-in defaults have been
    installed.

    Raises :class:`RuntimeError` when an entry-point key is not the expected
    ``"<slot>.<name>"`` shape, when two distributions register the same
    ``<slot>.<name>`` pair (a genuine ambiguity that must fail loud with both
    providers named), or when an entry point resolves to a non-class value.
    """
    seen: dict[str, importlib.metadata.EntryPoint] = {}
    registrations: dict[str, dict[str, type]] = {}
    for ep in importlib.metadata.entry_points(group=POLICIES_GROUP):
        existing = seen.get(ep.name)
        if existing is not None:
            raise RuntimeError(
                f"entry point {ep.name!r} in group {POLICIES_GROUP!r} is "
                f"registered by two distributions: "
                f"{_distribution_name(existing)!r} and "
                f"{_distribution_name(ep)!r}"
            )
        seen[ep.name] = ep
        if "." not in ep.name:
            raise RuntimeError(
                f"entry point {ep.name!r} in group {POLICIES_GROUP!r} must use "
                f"the '<slot>.<name>' shape; got a bare token"
            )
        slot, _, policy_name = ep.name.partition(".")
        if not slot or not policy_name:
            raise RuntimeError(
                f"entry point {ep.name!r} in group {POLICIES_GROUP!r} must use "
                f"the '<slot>.<name>' shape; got slot={slot!r} name={policy_name!r}"
            )
        cls = ep.load()
        if not isinstance(cls, type):
            raise RuntimeError(
                f"entry point {POLICIES_GROUP}.{ep.name!r} resolved to "
                f"{type(cls).__name__}, expected a class"
            )
        registrations.setdefault(slot, {})[policy_name] = cls
    return registrations


def _distribution_name(ep: importlib.metadata.EntryPoint) -> str:
    dist = ep.dist
    return dist.name if dist is not None else "<unknown distribution>"


def decode_models_fingerprint(state: dict[str, Any]) -> ModelsFingerprint | None:
    """Return the parsed fingerprint from an ``engine_run_state.json`` dict.

    * ``None`` when the ``models_fingerprint`` field is absent (older
      state files that predate this field).
    * :class:`ModelsFingerprint` when the field is a well-formed dict.
    * Raises :class:`pydantic.ValidationError` when the field is a dict
      but malformed — loud-fail matches the existing malformed-JSON
      behaviour of ``read_engine_run_state``.
    """
    raw = state.get("models_fingerprint")
    if raw is None:
        return None
    return ModelsFingerprint.model_validate(raw)
