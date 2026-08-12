"""Suite-scoped conftest for the capability probe bodies.

Owns:

* Fixture re-export — pulls :func:`live_client` and
  :func:`skip_unless_capability_declared` from
  :mod:`tolokaforge.testing.certify.fixtures` via ``pytest_plugins`` so
  bodies collect them without a second import.
* ``TF_PRESETS_FILE`` overlay hook — the model auto-integration re-probe
  step points this at the policy overlay the resolve agent set or
  created, so capability probes run under that policy's adapters
  (schema sanitizer / response / prompt) instead of the bundled default.
  The overlay is validated up front so a malformed policy fails loudly
  at startup, not silently as skipped probes. A normal CI run leaves
  ``TF_PRESETS_FILE`` unset and this is a no-op.
* Auto-marking — every test collected from this package is tagged with
  :func:`pytest.mark.integration`, :func:`pytest.mark.requires_api`,
  and :func:`pytest.mark.llm` so marker-gated pytest runs
  (``-m integration``) pick them up without individual files having to
  declare ``pytestmark`` lists.
"""

from __future__ import annotations

import os

import pytest

pytest_plugins = ["tolokaforge.testing.certify.fixtures"]

_AUTO_MARKERS = ("integration", "requires_api", "llm")
_SUITE_MODULE_PREFIX = "tolokaforge.testing.certify.suite."


def pytest_configure(config):  # type: ignore[no-untyped-def]
    """Install a preset overlay for the whole session when ``TF_PRESETS_FILE`` is set."""
    del config  # unused - the hook signature is fixed by pytest.
    overlay = os.getenv("TF_PRESETS_FILE")
    if not overlay:
        return
    from tolokaforge.core.llm import presets

    presets.validate_overlay_file(overlay)  # raises ValueError on a malformed overlay
    presets.set_overlay_path(overlay)


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """Tag every test in this suite with the marker-gated markers."""
    del config  # unused — the hook signature is fixed by pytest.
    for item in items:
        module_name = getattr(item.module, "__name__", "")
        if not module_name.startswith(_SUITE_MODULE_PREFIX):
            continue
        for marker in _AUTO_MARKERS:
            if not item.get_closest_marker(marker):
                item.add_marker(getattr(pytest.mark, marker))
