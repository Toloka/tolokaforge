"""Guards the rag-service image's baked embedding model.

The image carries ``all-MiniLM-L6-v2`` and runs ``HF_HUB_OFFLINE=1``, so startup
loads it from disk and contacts nothing. What holds that in place is pinned here
without a Docker daemon:

- the bake sits in the cache-stable slot, above every layer carrying tolokaforge
  source. Move it below the wheel ``COPY`` and every gate stays green while
  every build re-downloads 88MB — the one invariant here whose breakage is
  otherwise silent;
- one ``ARG`` feeds both the bake and the runtime ``ENV``, so the model the
  service loads cannot drift from the model the image carries, and its default
  is the same string as ``app.py``'s no-env fallback;
- ``HF_HOME`` is set before the bake (or the weights land where the runtime does
  not look) and ``HF_HUB_OFFLINE=1`` after it (or the bake itself is refused);
- the HEALTHCHECK grace covers the offline load rather than a download.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RAG_DOCKERFILE = _REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles" / "rag.Dockerfile"
_RAG_APP = _REPO_ROOT / "tolokaforge" / "env" / "rag_service" / "app.py"

# Offline startup measures 3.5s to serving; the grace covers that with headroom.
_MIN_START_PERIOD_SECONDS = 15

_START_PERIOD_RE = re.compile(r"--start-period=(\d+)s\b")
_ARG_EMBEDDING_MODEL_RE = re.compile(r"^ARG EMBEDDING_MODEL=(\S+)\s*$")
_DEFAULT_EMBEDDING_MODEL_RE = re.compile(r"^DEFAULT_EMBEDDING_MODEL = \"(\S+)\"\s*$", re.MULTILINE)


def _dockerfile_lines() -> list[str]:
    return _RAG_DOCKERFILE.read_text().splitlines()


def _sole_index(lines: list[str], predicate: Callable[[str], bool]) -> int:
    matches = [index for index, line in enumerate(lines) if predicate(line)]
    assert len(matches) == 1, (
        f"expected exactly one line in {_RAG_DOCKERFILE.name} matching this anchor, "
        f"found {len(matches)} at {matches}"
    )
    return matches[0]


def _bake_slot_index(lines: list[str]) -> int:
    return _sole_index(lines, lambda line: _ARG_EMBEDDING_MODEL_RE.match(line) is not None)


def _bake_run_index(lines: list[str]) -> int:
    return _sole_index(lines, lambda line: line.startswith("RUN python -c") and "Sentence" in line)


def test_bake_precedes_every_layer_carrying_tolokaforge_source() -> None:
    lines = _dockerfile_lines()
    wheel_arg = _sole_index(lines, lambda line: line.startswith("ARG WHEEL_FILENAME"))

    assert _bake_run_index(lines) < wheel_arg, (
        f"{_RAG_DOCKERFILE.name}: the `RUN` that downloads the model must precede "
        "`ARG WHEEL_FILENAME` — the wheel installed there carries app.py, so a bake below it "
        "re-downloads the model on every edit to any tolokaforge source file, silently and on "
        "every build. The layer is created by the RUN; the ARG above it creates none"
    )
    assert _bake_slot_index(lines) < wheel_arg, (
        f"{_RAG_DOCKERFILE.name}: `ARG EMBEDDING_MODEL` must stay above `ARG WHEEL_FILENAME` "
        "with the RUN it feeds, so the bake layer's cache key carries no tolokaforge source"
    )


def test_offline_mode_and_runtime_model_are_set_after_the_bake() -> None:
    lines = _dockerfile_lines()
    bake_slot = _bake_slot_index(lines)
    bake_run = _bake_run_index(lines)
    offline = _sole_index(lines, lambda line: line.startswith("ENV HF_HUB_OFFLINE="))
    runtime_model = _sole_index(lines, lambda line: line.startswith("ENV EMBEDDING_MODEL="))
    hf_home = _sole_index(lines, lambda line: line.startswith("ENV HF_HOME="))

    assert "${EMBEDDING_MODEL}" in lines[bake_run], (
        f"{_RAG_DOCKERFILE.name}: the bake must materialise the ARG's model, not a literal — "
        "a literal here bakes one model while the runtime ENV names another"
    )
    assert bake_slot < hf_home < bake_run, (
        f"{_RAG_DOCKERFILE.name}: HF_HOME must be set before the bake or the weights land outside "
        "the cache the runtime reads"
    )
    assert bake_run < offline, (
        f"{_RAG_DOCKERFILE.name}: HF_HUB_OFFLINE must be set after the bake — set before it, the "
        "bake itself is refused and the build fails"
    )
    assert bake_run < runtime_model, (
        f"{_RAG_DOCKERFILE.name}: the runtime EMBEDDING_MODEL must be set after the bake, so "
        "it carries the ARG the bake resolved"
    )
    assert lines[offline] == "ENV HF_HUB_OFFLINE=1", (
        f"{_RAG_DOCKERFILE.name}: offline mode must be on, or startup reaches for HuggingFace "
        "again and the baked weights buy nothing"
    )


def test_runtime_model_interpolates_the_bake_arg() -> None:
    lines = _dockerfile_lines()
    runtime_model = _sole_index(lines, lambda line: line.startswith("ENV EMBEDDING_MODEL="))

    assert lines[runtime_model] == "ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}", (
        f"{_RAG_DOCKERFILE.name}: the runtime ENV must interpolate the ARG the bake used. A "
        "repeated literal is the drift the single ARG exists to make impossible — the service "
        "would load a model the image does not carry, and offline mode would fail that load"
    )


def test_bake_arg_default_matches_the_services_default_model() -> None:
    lines = _dockerfile_lines()
    arg_match = _ARG_EMBEDDING_MODEL_RE.match(lines[_bake_slot_index(lines)])
    assert arg_match is not None

    app_match = _DEFAULT_EMBEDDING_MODEL_RE.search(_RAG_APP.read_text())
    assert app_match is not None, f"{_RAG_APP.name} has no DEFAULT_EMBEDDING_MODEL to compare"

    assert arg_match.group(1) == app_match.group(1), (
        "the baked model and app.py's DEFAULT_EMBEDDING_MODEL have drifted — the constant is the "
        "documented no-env fallback, so a build whose ARG differs bakes one model and falls back "
        "to another"
    )


def test_healthcheck_start_period_covers_the_offline_load() -> None:
    text = _RAG_DOCKERFILE.read_text()
    assert "HEALTHCHECK" in text, f"{_RAG_DOCKERFILE} has no HEALTHCHECK to guard"

    match = _START_PERIOD_RE.search(text)
    assert match is not None, (
        f"{_RAG_DOCKERFILE.name} HEALTHCHECK has no `--start-period=<n>s` — the probe would start "
        "enforcing immediately and fail the stack while the embedding model loads"
    )

    start_period = int(match.group(1))
    assert start_period >= _MIN_START_PERIOD_SECONDS, (
        f"{_RAG_DOCKERFILE.name} HEALTHCHECK --start-period={start_period}s is below the "
        f"{_MIN_START_PERIOD_SECONDS}s floor over the measured offline model load"
    )
