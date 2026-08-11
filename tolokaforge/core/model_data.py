"""Public engine seam for bundled model-data resources.

The module exposes three related surfaces:

* Three path accessors — :func:`bundled_pricing_path`,
  :func:`bundled_presets_path`, :func:`bundled_providers_path` — that
  return the on-disk location of the bundled model-data files
  (``pricing.json``, ``model_presets.yaml``, ``providers.yaml``) shipped
  by the installed :mod:`tolokaforge_models` wheel. Consumers parse and
  validate the file's contents themselves; the accessors only guarantee
  the file exists.
* :func:`bundled_certificates` — the tuple of
  :class:`~tolokaforge.testing.certify.ModelCertificate` instances the
  installed :mod:`tolokaforge_models` wheel ships. The accessor is the
  single entry point the certify seam re-exposes as
  :data:`tolokaforge.testing.certify.ALL_MODELS`.
* The fingerprint schema types (:class:`ModelsFingerprint`,
  :data:`MODELS_FINGERPRINT_API_VERSION`) plus
  :func:`decode_models_fingerprint`, the read-side decoder used by
  :mod:`tolokaforge.core.engine_run_state`. The package version and
  minimum-engine-version strings persisted on the fingerprint are
  sourced from :data:`tolokaforge_models.__version__` and
  :data:`tolokaforge_models.minimum_engine_version` at compute time —
  see :mod:`tolokaforge.core.model_data_fingerprint`.

The module has **no first-party imports** — it is safe to import from
runner-subset code. :func:`load_policy_registrations` inlines its
``importlib.metadata`` enumeration (rather than routing through
:mod:`tolokaforge.core.plugin_registry`) so this module's import graph stays
light and free of the presets-side circular reach the plugin registry has via
:mod:`tolokaforge.core.loop`. :func:`bundled_certificates` defers its
``tolokaforge_models.certificates`` import into the function body for the
same reason. The fingerprint compute path (which needs the resolved preset
table, pricing dict, provider bindings, and certificate registry) lives in
the orchestrator-only sibling :mod:`tolokaforge.core.model_data_fingerprint`.

``_DATA_ROOT`` is the internal seam pointing at the ``tolokaforge_models``
wheel's ``data/`` directory. Tests monkey-patch this constant to redirect
the accessors at a scratch tree.

:func:`_check_minimum_engine_version` is the install-time gate the engine
fires at :mod:`tolokaforge.core.llm.presets` import — refuses to boot when
:mod:`tolokaforge_models` is missing or when the installed engine version
falls outside :data:`tolokaforge_models.minimum_engine_version`. The engine
version is resolved via :func:`_resolve_engine_version`, which tries the
``tolokaforge`` distribution first and falls back to
``tolokaforge-runner-subset`` so the same gate fires unmodified inside the
runner subset image.

See ADR-0030 § "The one seam", § "Fingerprinting for auditability", and
§ "Install-time validation" for the wheel-split context.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

if TYPE_CHECKING:
    from tolokaforge.testing.certify import ModelCertificate

__all__ = [
    "MODELS_FINGERPRINT_API_VERSION",
    "ModelsFingerprint",
    "bundled_certificates",
    "bundled_presets_path",
    "bundled_pricing_path",
    "bundled_providers_path",
    "decode_models_fingerprint",
    "load_policy_registrations",
]

#: Distribution names the engine is known to ship under. The base wheel is
#: ``tolokaforge``; the Docker-only runner subset is
#: ``tolokaforge-runner-subset`` (both install the same top-level package but
#: register different distribution metadata). :func:`_check_minimum_engine_version`
#: probes them in order so the install-time gate works in both wheels — a
#: subset image where ``importlib.metadata.version("tolokaforge")`` would raise
#: ``PackageNotFoundError`` still resolves to the subset wheel's declared
#: version, and the check fires against that.
_ENGINE_DISTRIBUTION_CANDIDATES: Final[tuple[str, ...]] = (
    "tolokaforge",
    "tolokaforge-runner-subset",
)

#: Entry-point group each installed models wheel declares its per-model policy
#: subclasses under. See :func:`load_policy_registrations`.
POLICIES_GROUP: Final[str] = "tolokaforge.policies"


_DATA_ROOT: Final[Path] = Path(str(importlib.resources.files("tolokaforge_models") / "data"))
"""Internal seam pointing at the ``tolokaforge_models`` wheel's ``data/``
directory. The three accessors resolve their targets under this root;
tests monkey-patch this constant to redirect them at a scratch tree."""


def bundled_pricing_path() -> Path:
    """Return the on-disk path of the bundled ``pricing.json`` table.

    Raises :class:`FileNotFoundError` when the file is absent — a
    corrupted install shape the caller must surface, not swallow. The
    accessor does **not** open, parse, or validate the file; the
    consumer (``tolokaforge.core.pricing._load_pricing``) is responsible
    for rejecting empty / malformed content with its own loud failure.

    Public API. Stable within v0.17.x — downstream code that reads the
    bundled pricing table should use this accessor instead of reaching
    into ``tolokaforge_models/data/`` directly.
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
    bundled preset table should use this accessor instead of reaching
    into ``tolokaforge_models/data/`` directly.
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
    bundled provider table should use this accessor instead of reaching
    into ``tolokaforge_models/data/`` directly.
    """
    path = _DATA_ROOT / "providers.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled provider table not found at {path} — corrupted install?")
    return path


def bundled_certificates() -> tuple[ModelCertificate, ...]:
    """Return the certificate tuple shipped by the installed ``tolokaforge-models`` wheel.

    Raises :class:`ImportError` when no ``tolokaforge-models`` is installed
    — the certify seam surfaces that as a startup failure. The accessor
    performs no validation of the tuple's contents; the models wheel
    enforces uniqueness and slug-agreement at its own import time.

    Public API. Stable within v0.17.x — downstream code that needs the
    full certificate table should reach for this accessor rather than
    importing the private registry module directly.
    """
    from tolokaforge_models.certificates import ALL_MODELS

    return ALL_MODELS


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
        The ``tolokaforge-models`` PEP 440 version — sourced from
        :data:`tolokaforge_models.__version__` at compute time.
    content_sha256:
        Lowercase hex sha256 over the canonicalised
        ``{presets, pricing, providers, certificates}`` payload. Same
        inputs → byte-identical digest; any overlay tweak → different
        digest.
    api_version:
        Contract version of the hashed payload — see
        :data:`MODELS_FINGERPRINT_API_VERSION`.
    minimum_engine_version:
        PEP 440 specifier the model-data snapshot requires the engine to
        satisfy — sourced from
        :data:`tolokaforge_models.minimum_engine_version` at compute time.
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


def _resolve_engine_version() -> str:
    """Return the PEP 440 version of the installed engine distribution.

    Tries every name in :data:`_ENGINE_DISTRIBUTION_CANDIDATES` in order; the
    first ``importlib.metadata.version`` call that succeeds wins. The base
    wheel ships as ``tolokaforge`` and the Docker-only runner subset ships as
    ``tolokaforge-runner-subset``; either satisfies the check.

    Raises :class:`RuntimeError` when none of the candidates resolve — a
    corrupted install shape the caller must surface, not swallow.
    """
    for name in _ENGINE_DISTRIBUTION_CANDIDATES:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise RuntimeError(
        f"engine version cannot be resolved: none of "
        f"{list(_ENGINE_DISTRIBUTION_CANDIDATES)} report a distribution "
        f"version via importlib.metadata. Reinstall the engine wheel."
    )


def _check_minimum_engine_version() -> None:
    """Refuse to boot without ``tolokaforge-models``, or on an engine below its floor.

    Called at ``tolokaforge.core.llm.presets`` import time — before any
    ``RunConfig`` load — so a bad install pair surfaces as a startup
    ``RuntimeError`` rather than a silent-wrong first LLM call. Two failure
    branches:

    * ``tolokaforge_models`` is not importable → raises with the
      ``pip install tolokaforge-models`` install instruction, chained from the
      underlying :class:`ImportError`.
    * The installed engine version does not satisfy
      :data:`tolokaforge_models.minimum_engine_version` → raises naming both
      the resolved engine version and the models-wheel floor specifier.

    See ADR-0030 § "Install-time validation".
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    try:
        import tolokaforge_models
    except ImportError as exc:
        raise RuntimeError(
            "tolokaforge requires tolokaforge-models >= 1.0.0. "
            "Install with `pip install tolokaforge-models`."
        ) from exc

    floor = SpecifierSet(tolokaforge_models.minimum_engine_version)
    installed = Version(_resolve_engine_version())
    if installed not in floor:
        raise RuntimeError(
            f"tolokaforge-models {tolokaforge_models.__version__} requires "
            f"tolokaforge {tolokaforge_models.minimum_engine_version}; "
            f"installed {installed}. Upgrade the engine or downgrade the "
            f"models wheel."
        )


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
