"""The three doc surfaces a new-model contributor reads carry current paths.

``docs/ADD_NEW_MODEL.md`` walks the six steps end-to-end,
``docs/AUTO_INTEGRATION.md`` describes what the auto-integration workflow
writes on their behalf, and ``AGENTS.md`` § "Adding a new model / provider"
carries the non-negotiable rules. The three docs must name the models-wheel
paths under the src-layout — the layout the finalize agent writes into and
the layout ``tolokaforge_models/`` actually publishes.

Three forbidden strings and one link-resolution assertion make the freshness
grep-visible instead of relying on a rendered read:

1. ``tolokaforge_models/data/`` without a preceding ``src/tolokaforge_models/``
   segment — pre-src-layout drift.
2. ``tolokaforge/core/data/`` — removed by #938; a hit is stale narrative in a
   living doc.
3. ``tolokaforge/testing/certify/_registry.py`` — removed by #938; the module
   lives at
   ``tolokaforge_models/src/tolokaforge_models/certificates/registry.py``.
4. Every ``[label](target)`` link whose target ends in ``.py`` / ``.json`` /
   ``.yaml`` / ``.md`` resolves to a real file — a src-layout typo (missing
   segment, extra segment) reds this test instead of surviving unnoticed.

Historical narrative in ``docs/adr/`` is intentionally out of scope: an ADR
freezes the state it recorded, so a path a later cutover moved is history in
that document. The sweep runs only over the three living docs above.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_DOCS = (
    _REPO_ROOT / "docs" / "ADD_NEW_MODEL.md",
    _REPO_ROOT / "docs" / "AUTO_INTEGRATION.md",
    _REPO_ROOT / "AGENTS.md",
)
_LINK_TARGET = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_LINKABLE_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".md")
_LEGACY_DATA_SEGMENT = re.compile(r"(?<!src/)tolokaforge_models/data/")
_LEGACY_ENGINE_DATA = re.compile(r"tolokaforge/core/data/")
_LEGACY_REGISTRY_FILE = re.compile(r"tolokaforge/testing/certify/_registry\.py")


@pytest.mark.parametrize("doc", _TARGET_DOCS, ids=lambda p: p.name)
def test_no_pre_src_layout_models_wheel_data_path(doc: Path) -> None:
    """No line names ``tolokaforge_models/data/`` without the src-layout prefix.

    The finalize agent writes into
    ``tolokaforge_models/src/tolokaforge_models/data/``, and every user-facing
    reference must match — a bare ``tolokaforge_models/data/`` in a
    contributor-facing doc sends the reader to a path that does not exist.
    """
    hits = [
        f"{doc.name}:{n}: {line}"
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        if _LEGACY_DATA_SEGMENT.search(line)
    ]
    message = f"pre-src-layout `tolokaforge_models/data/` path in {doc.name}:\n" + "\n".join(hits)
    assert hits == [], message


@pytest.mark.parametrize("doc", _TARGET_DOCS, ids=lambda p: p.name)
def test_no_pre_cutover_engine_data_path(doc: Path) -> None:
    """No line names ``tolokaforge/core/data/`` — that directory was removed."""
    hits = [
        f"{doc.name}:{n}: {line}"
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        if _LEGACY_ENGINE_DATA.search(line)
    ]
    message = f"pre-#938 `tolokaforge/core/data/` path in {doc.name}:\n" + "\n".join(hits)
    assert hits == [], message


@pytest.mark.parametrize("doc", _TARGET_DOCS, ids=lambda p: p.name)
def test_no_pre_cutover_certificate_registry_path(doc: Path) -> None:
    """No line names ``tolokaforge/testing/certify/_registry.py`` — file gone."""
    hits = [
        f"{doc.name}:{n}: {line}"
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        if _LEGACY_REGISTRY_FILE.search(line)
    ]
    message = (
        f"pre-#938 `tolokaforge/testing/certify/_registry.py` path in "
        f"{doc.name}:\n" + "\n".join(hits)
    )
    assert hits == [], message


@pytest.mark.parametrize("doc", _TARGET_DOCS, ids=lambda p: p.name)
def test_every_file_link_target_resolves(doc: Path) -> None:
    """Every ``.py`` / ``.json`` / ``.yaml`` / ``.md`` link target exists.

    A ``[label](path)`` link is a promise that ``path`` — resolved relative to
    the document — points at something. A src-layout typo (missing
    ``src/tolokaforge_models/`` segment, extra segment) breaks the link
    without breaking the doc's grep-visible words, so the walker locks it
    here.
    """
    text = doc.read_text(encoding="utf-8")
    missing: list[str] = []
    for target in _LINK_TARGET.findall(text):
        # Drop anchor and query, ignore external URLs and mailto links.
        raw = target.split("#", 1)[0].split("?", 1)[0]
        if not raw or raw.startswith(("http://", "https://", "mailto:")):
            continue
        if not raw.endswith(_LINKABLE_SUFFIXES):
            continue
        resolved = (doc.parent / raw).resolve()
        if not resolved.exists():
            missing.append(f"{doc.name}: [...]({target}) -> {resolved}")
    message = f"unresolved file link(s) in {doc.name}:\n" + "\n".join(missing)
    assert missing == [], message
