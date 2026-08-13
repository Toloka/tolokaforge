"""Regression: container reuse must be robust to Docker tag ordering.

The v1 pull path aliases the underlying image with
``tolokaforge-runner:local`` on top of the pulled
``tolokasoft1/tolokaforge-runner:X.Y.Z`` tag. Docker's daemon does not
guarantee the order of ``image.tags`` — so a comparison against
``existing.image.tags[0]`` alone is a coin flip: sometimes it returns
the alias, sometimes the pulled tag. The pre-fix comparison would then
spuriously destroy a healthy container on every subsequent run.

Fix: ``_start_service`` matches on image-id OR membership of the
expected ``full_tag`` in the container image's tag list. This test pins
that behaviour on both axes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models.docker_config import DockerConfig
from tolokaforge.docker.container import Container, ContainerStatus
from tolokaforge.docker.image import Image
from tolokaforge.docker.stack import EngineStack, ServiceDefinition

pytestmark = pytest.mark.unit


PUBLISHED_TAG = "tolokasoft1/tolokaforge-runner:0.18.0"
LOCAL_ALIAS = "tolokaforge-runner:local"
IMAGE_ID = "sha256:abc123"


def _stack_with_running_container(
    monkeypatch: pytest.MonkeyPatch,
    *,
    published_tag: str,
    image_id: str,
    existing_tags: list[str],
    existing_image_id: str,
) -> tuple[EngineStack, MagicMock]:
    """Assemble an EngineStack where _try_reuse_existing returns a
    running Container, and _inspect_running_image returns the daemon
    state under test. Returns (stack, destroy_mock)."""
    svc = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        published_image_repo="tolokasoft1/tolokaforge-runner",
        dockerfile="docker/runner.Dockerfile",
        context=".",
    )
    stack = EngineStack(config=DockerConfig())
    stack.add_service(svc)

    # Populate the _images map so _start_service does not re-build.
    pulled_image = Image(
        name="tolokasoft1/tolokaforge-runner",
        tag="0.18.0",
        image_id=image_id,
        dockerfile="pulled",
        context="pulled",
        context_hash="pulled",
    )
    assert pulled_image.full_tag == published_tag
    stack._images["runner"] = pulled_image

    # Stub Container returned by _try_reuse_existing. Container is a
    # frozen-ish Pydantic model with extra=forbid; monkeypatch the
    # ``destroy`` method via ``object.__setattr__`` so we can observe
    # calls without touching the model schema.
    destroy_mock = MagicMock()
    fake_container = Container(
        container_id="cid-existing",
        name="tolokaforge-runner",
        image_tag=existing_tags[0] if existing_tags else "unknown",
        current_status=ContainerStatus.RUNNING,
    )
    object.__setattr__(fake_container, "destroy", destroy_mock)

    monkeypatch.setattr(stack, "_try_reuse_existing", lambda *a, **kw: fake_container)
    monkeypatch.setattr(
        stack,
        "_inspect_running_image",
        lambda name: (list(existing_tags), existing_image_id),
    )
    # Suppress the LogRouter attach — the fake Container has no real docker
    # client behind it, so LogRouter.for_container would fail.
    monkeypatch.setattr(
        "tolokaforge.docker.stack.LogRouter.for_container",
        classmethod(lambda cls, *a, **kw: MagicMock()),
    )
    return stack, destroy_mock


class TestReuseWhenPulledTagIsSecondaryInTagList:
    """The pulled image has BOTH the pulled tag and the ``:local`` alias.
    If the daemon returns the alias in ``tags[0]``, the pre-fix code
    would destroy the healthy container. The fix compares membership /
    image id instead."""

    def test_reused_when_alias_first_and_pulled_tag_second(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stack, destroy_mock = _stack_with_running_container(
            monkeypatch,
            published_tag=PUBLISHED_TAG,
            image_id=IMAGE_ID,
            existing_tags=[LOCAL_ALIAS, PUBLISHED_TAG],
            existing_image_id=IMAGE_ID,
        )
        svc = stack.services["runner"]

        # Drive just the reuse-check block by triggering _start_service.
        # We stub the downstream network/container-creation path so the
        # test aborts once we've decided whether to reuse or destroy.
        try:
            stack._start_service(svc, wait=False)
        except Exception:  # noqa: BLE001 — network / container-run stubs abort us
            pass

        destroy_mock.assert_not_called()

    def test_reused_when_pulled_tag_first_and_alias_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, destroy_mock = _stack_with_running_container(
            monkeypatch,
            published_tag=PUBLISHED_TAG,
            image_id=IMAGE_ID,
            existing_tags=[PUBLISHED_TAG, LOCAL_ALIAS],
            existing_image_id=IMAGE_ID,
        )
        svc = stack.services["runner"]

        try:
            stack._start_service(svc, wait=False)
        except Exception:  # noqa: BLE001 — abort past reuse check
            pass

        destroy_mock.assert_not_called()

    def test_reused_when_only_alias_but_ids_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fallback path: even if the daemon has forgotten the
        ``tolokasoft1/...`` tag entirely (e.g. an operator manually
        retagged), image id equality still identifies the reuse."""
        stack, destroy_mock = _stack_with_running_container(
            monkeypatch,
            published_tag=PUBLISHED_TAG,
            image_id=IMAGE_ID,
            existing_tags=[LOCAL_ALIAS],
            existing_image_id=IMAGE_ID,
        )
        svc = stack.services["runner"]

        try:
            stack._start_service(svc, wait=False)
        except Exception:  # noqa: BLE001 — abort past reuse check
            pass

        destroy_mock.assert_not_called()

    def test_destroyed_when_neither_tags_nor_ids_match(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Sanity: a truly stale container (different image entirely)
        must still be destroyed."""
        stack, destroy_mock = _stack_with_running_container(
            monkeypatch,
            published_tag=PUBLISHED_TAG,
            image_id=IMAGE_ID,
            existing_tags=["some-other-image:latest"],
            existing_image_id="sha256:stale-old-image",
        )
        svc = stack.services["runner"]

        try:
            stack._start_service(svc, wait=False)
        except Exception:  # noqa: BLE001 — abort past reuse check
            pass

        destroy_mock.assert_called_once()

    def test_destroyed_when_daemon_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _inspect_running_image returns ([], None) — e.g. daemon
        blip — err on the side of recreating."""
        stack, destroy_mock = _stack_with_running_container(
            monkeypatch,
            published_tag=PUBLISHED_TAG,
            image_id=IMAGE_ID,
            existing_tags=[],
            existing_image_id=None,  # type: ignore[arg-type]
        )
        svc = stack.services["runner"]

        try:
            stack._start_service(svc, wait=False)
        except Exception:  # noqa: BLE001 — abort past reuse check
            pass

        destroy_mock.assert_called_once()
