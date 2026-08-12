"""The deploy lane's compose waits, and why the two image sources differ.

An image built from this tree carries the rag-service embedding model and stands
up without contacting HuggingFace, so the lanes driving local images wait only
for a four-service bring-up. The published lane pulls
``tolokasoft1/tolokaforge-rag-service:latest``, which predates that bake and
still downloads ``all-MiniLM-L6-v2`` on first start, so its wait must stay
download-sized until a stable publish carries the bake.

That asymmetry is invisible in a passing run — both lanes are green either way on
a machine with a warm cache and a fast link — and a flat reduction would red the
published lane on a cold runner. This pins it without a Docker daemon.
"""

from __future__ import annotations

import pytest

from tests.integration.deploy.test_standalone_compose import (
    _COMPOSE_WAIT_TIMEOUT_S as _COMPOSE_LANE_WAITS,
)
from tests.integration.deploy.test_standalone_compose import composed_stack
from tests.integration.deploy.test_standalone_example import (
    _COMPOSE_WAIT_TIMEOUT_S as _EXAMPLE_LANE_WAIT,
)

pytestmark = pytest.mark.canonical

_DOWNLOAD_SIZED_S = 300
_BRING_UP_SIZED_S = 120


def test_every_image_source_the_lane_drives_has_a_wait() -> None:
    modes = set(composed_stack._fixture_function_marker.params)

    assert modes == set(_COMPOSE_LANE_WAITS), (
        "every mode the stack fixture is parametrized over must have a wait of its own — the "
        f"lane looks the mode up by key, so a mode without one raises KeyError at bring-up "
        f"(fixture params {sorted(modes)}, waits {sorted(_COMPOSE_LANE_WAITS)})"
    )


def test_published_lane_keeps_a_download_sized_wait() -> None:
    assert _COMPOSE_LANE_WAITS["published"] == _DOWNLOAD_SIZED_S, (
        "the published lane pulls an image that still downloads all-MiniLM-L6-v2 on first "
        "start, so its wait stays download-sized. It comes down when a stable publish is cut "
        "from a commit carrying the bake, not before — reducing it here reds the lane on a "
        "cold runner"
    )


def test_local_compose_lane_waits_only_for_a_bring_up() -> None:
    assert _COMPOSE_LANE_WAITS["local"] == _BRING_UP_SIZED_S, (
        "images built from this tree carry the embedding model, so the local lane budgets for "
        "a four-service bring-up rather than a download"
    )


def test_example_driver_lane_waits_only_for_a_bring_up() -> None:
    assert _EXAMPLE_LANE_WAIT == _BRING_UP_SIZED_S, (
        "the example-driver lane builds and drives `:local` images only, so it carries the "
        "same bring-up-sized wait as the local compose lane"
    )
