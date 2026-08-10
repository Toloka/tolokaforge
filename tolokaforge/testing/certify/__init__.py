"""Public engine seam for per-model capability certification.

External callers ``pip install tolokaforge`` and drive the capability
probe suite against their own model list by importing the data types
here and, in later stages, the shared fixtures + probe-registration
decorator.

Consumers reach for:

* :class:`Capability` — every behavioural contract a live LLM route may
  or may not honour.
* :class:`ModelCertificate` — frozen per-model declaration of which
  capabilities are required, which are known-unsupported, and per-model
  exclusions / rationales / probe parameter overrides.
* :data:`ALL_MODELS` — the tuple of certificates the shared suite
  parametrises over.
"""

from ._capability import Capability
from ._registry import ALL_MODELS
from .certificate import ModelCertificate

__all__ = [
    "ALL_MODELS",
    "Capability",
    "ModelCertificate",
]
