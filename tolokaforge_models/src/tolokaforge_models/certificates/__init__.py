"""Registry of :class:`tolokaforge.testing.certify.ModelCertificate` instances.

The engine reaches this tuple through
:func:`tolokaforge.core.model_data.bundled_certificates`, which is
re-exposed as :data:`tolokaforge.testing.certify.ALL_MODELS` at the
public certify seam. Adding a new certificate is an edit to
:mod:`tolokaforge_models.certificates.registry`.
"""

from __future__ import annotations

from tolokaforge_models.certificates.registry import ALL_MODELS

__all__ = ["ALL_MODELS"]
