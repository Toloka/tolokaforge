"""Bucket A/B classifier for the models-wheel taxonomy.

Reads the set of files a commit or a staged diff touched and decides
whether the change is Bucket A (data / cert / models-wheel content only,
zero engine-side change) or Bucket B (any touched file falls outside the
Bucket-A allow-list). Never opens a git repository and never reads any
of the files it classifies; the input is a plain iterable of path
strings.

The primitive is the sole source of truth for the ADR-0030 bucket split
and is shared by two callers:

- ``tests/canonical/test_models_wheel_replay.py`` — replays every
  ``^integrate: `` commit reachable from HEAD and asserts the historical
  distribution against a canonical snapshot.
- ``.github/workflows/integrate-model.yml`` — invokes
  ``automation classify-paths --paths-from-cached`` at finalize time as a
  commit gate: Bucket A commits, with the subject and the Slack message
  naming the bucket; Bucket B is refused and routed to needs-human.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class Bucket(str, Enum):
    """Outcome of classifying a touched-file set."""

    A = "A"
    B = "B"


# The cert registry lives under exactly one path in the tree at any given
# time. Both the current location and the pre-move location are listed
# so a replay across the file move classifies touches to either as cert
# data — the file's identity, not its filesystem path, is the invariant.
BUCKET_A_ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "tests/integration/llm/registry.py",
        "tolokaforge/testing/certify/_registry.py",
        "tolokaforge/core/data/model_presets.yaml",
        "tolokaforge/core/data/pricing.json",
    }
)


# Prefix allow-list — any file whose path starts with one of these
# prefixes is treated as data / models-wheel content. Extend when a
# milestone PR lands a new data category.
BUCKET_A_ALLOWED_PREFIXES: tuple[str, ...] = (
    "tolokaforge_models/",
    "tolokaforge/core/data/",
    "tests/fixtures/probe_responses/",
)


@dataclass(frozen=True)
class Classification:
    """Result of classifying a touched-file set."""

    bucket: Bucket
    reason: str
    engine_paths: tuple[str, ...]


_REASON_EMPTY = "empty diff"
_REASON_ALL_ALLOWED = "all touched paths in Bucket-A allow-list"
_REASON_ENGINE_TEMPLATE = "{n} engine-side path(s) outside Bucket-A allow-list"


def _is_bucket_a_path(path: str) -> bool:
    if path in BUCKET_A_ALLOWED_FILES:
        return True
    return any(path.startswith(prefix) for prefix in BUCKET_A_ALLOWED_PREFIXES)


def classify_paths(touched: Iterable[str]) -> Classification:
    """Classify a touched-file set as Bucket A or Bucket B.

    Every path on the allow-list -> Bucket A. Any path outside the
    allow-list -> Bucket B with those offending paths surfaced (sorted,
    deduped) via ``Classification.engine_paths``. An empty input is
    Bucket A with empty ``engine_paths``.
    """
    unique_paths = sorted(set(touched))
    if not unique_paths:
        return Classification(bucket=Bucket.A, reason=_REASON_EMPTY, engine_paths=())
    engine_paths = tuple(p for p in unique_paths if not _is_bucket_a_path(p))
    if not engine_paths:
        return Classification(bucket=Bucket.A, reason=_REASON_ALL_ALLOWED, engine_paths=())
    reason = _REASON_ENGINE_TEMPLATE.format(n=len(engine_paths))
    return Classification(bucket=Bucket.B, reason=reason, engine_paths=engine_paths)
