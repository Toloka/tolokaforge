"""Data-vs-code taxonomy classifier for the coding-harness surface.

Analog of :mod:`automation.bucket_classifier` for coding-harness
commits. Reads the set of files a commit touched and decides whether
the change is Bucket A (data / example config / doc only — the
extension surface a plugin bundle would exercise) or Bucket B (any
touched file falls outside the Bucket-A allow-list — Python or shell
that lives in the adapter or in a test that exercises adapter code).

The primitive measures progress in migrating the harness surface from
code to data — an ongoing hygiene metric independent of any future
packaging decision. It feeds one caller today:

- ``tests/canonical/test_harness_registry_replay.py`` — replays every
  commit reachable from HEAD that touched the harness surface and
  asserts the historical distribution against a canonical snapshot.

Never opens a git repository and never reads any of the files it
classifies; the input is a plain iterable of path strings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class HarnessBucket(str, Enum):
    """Outcome of classifying a touched-file set on the harness surface."""

    A = "A"
    B = "B"


# Each ADR here documents the harness surface rather than implementing it, so
# an ADR-only commit is a data/doc move; omitting one classifies that commit as
# Bucket B and understates the migration the metric measures.
BUCKET_A_ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "external_adapters/tolokaforge-adapter-terminal-bench/README.md",
        "docs/adr/0033-external-harness-registry.md",
        "docs/adr/0034-external-harness-plugin-discovery.md",
        "docs/adr/0036-tolokaforge-coding-harnesses-split.md",
        "docs/adr/0037-runtime-gateway-as-harness-data.md",
    }
)


BUCKET_A_ALLOWED_PREFIXES: tuple[str, ...] = (
    # Shipped YAML data (harnesses.yaml, registry_meta.yaml, and
    # anything else the harness package ships under data/ later).
    "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/",
    # Where that data lived before ADR-0036 moved it out of the adapter.
    # Retained so historical commits keep the classification they earned:
    # repointing rather than adding would reclassify the pre-move Bucket-A
    # commits as B and zero the metric.
    "external_adapters/tolokaforge-adapter-terminal-bench/src/"
    "tolokaforge_adapter_terminal_bench/data/",
    # The two canonical-snapshot trees the harness surface pins. Regenerating
    # a snapshot after a data change is expected to be an A move; it is the
    # commit intentionally locking the new shape.
    "tests/canonical/snapshots/tbench_echo_hello_harness/",
    "tests/canonical/snapshots/tbench_echo_hello_skills_harness/",
    # Example run configs + overlays that ship alongside the adapter.
    "examples/terminal_bench/",
)


@dataclass(frozen=True)
class HarnessClassification:
    """Result of classifying a touched-file set on the harness surface."""

    bucket: HarnessBucket
    reason: str
    adapter_paths: tuple[str, ...]


_REASON_EMPTY = "empty diff"
_REASON_ALL_ALLOWED = "all touched paths in Bucket-A allow-list"
_REASON_ADAPTER_TEMPLATE = "{n} adapter-side path(s) outside Bucket-A allow-list"


def _is_bucket_a_path(path: str) -> bool:
    if path in BUCKET_A_ALLOWED_FILES:
        return True
    return any(path.startswith(prefix) for prefix in BUCKET_A_ALLOWED_PREFIXES)


def classify_harness_paths(touched: Iterable[str]) -> HarnessClassification:
    """Classify a touched-file set as Bucket A or Bucket B.

    Every path on the allow-list -> Bucket A. Any path outside the
    allow-list -> Bucket B with those offending paths surfaced (sorted,
    deduped) via ``HarnessClassification.adapter_paths``. An empty input
    is Bucket A with empty ``adapter_paths``.
    """
    unique_paths = sorted(set(touched))
    if not unique_paths:
        return HarnessClassification(bucket=HarnessBucket.A, reason=_REASON_EMPTY, adapter_paths=())
    adapter_paths = tuple(p for p in unique_paths if not _is_bucket_a_path(p))
    if not adapter_paths:
        return HarnessClassification(
            bucket=HarnessBucket.A, reason=_REASON_ALL_ALLOWED, adapter_paths=()
        )
    reason = _REASON_ADAPTER_TEMPLATE.format(n=len(adapter_paths))
    return HarnessClassification(bucket=HarnessBucket.B, reason=reason, adapter_paths=adapter_paths)
