"""Public engine seam for per-model capability certification.

External callers ``pip install tolokaforge`` and drive the capability
probe suite against their own model list by importing the data types
and probe-registration decorator here.

Consumers reach for:

* :class:`Capability` — every behavioural contract a live LLM route may
  or may not honour.
* :class:`ModelCertificate` — frozen per-model declaration of which
  capabilities are required, which are known-unsupported, and per-model
  exclusions / rationales / probe parameter overrides.
* :data:`ALL_MODELS` — the tuple of certificates the shared suite
  parametrises over. Resolved on first access from
  :func:`tolokaforge.core.model_data.bundled_certificates`, which reads
  the tuple out of the installed :mod:`tolokaforge_models` wheel.
* :func:`register_probe`, :func:`get_probe` — per-capability probe
  registry for out-of-tree probe bodies scoped to one ``model_id`` or
  used as the capability-wide default.

Pytest fixtures (``live_client``, ``skip_unless_capability_declared``)
are not re-exported here — importing them at the package level would
require every runtime caller of this seam to install ``pytest``. Suite
authors reach them via ``pytest_plugins =
["tolokaforge.testing.certify.fixtures"]`` in their own conftest, or by
importing the submodule directly as
``from tolokaforge.testing.certify.fixtures import live_client``.
"""

from typing import Any

from ._capability import Capability
from .certificate import ModelCertificate
from .probes import get_probe, register_probe

__all__ = [
    "ALL_MODELS",
    "Capability",
    "ModelCertificate",
    "get_probe",
    "register_probe",
]


def __getattr__(name: str) -> Any:
    if name == "ALL_MODELS":
        from tolokaforge.core.model_data import bundled_certificates

        value = bundled_certificates()
        globals()["ALL_MODELS"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
