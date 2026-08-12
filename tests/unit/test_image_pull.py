"""Unit tests for ``Image.pull()`` and ``ImagePullError``.

Every test uses a mocked ``docker.DockerClient`` — no daemon is touched,
so this file is safe to run under any environment. The mock shape
mirrors ``tests/unit/test_network_409_race.py`` (the same pattern is
used across the docker unit suite).

The retry harness inside ``Image.pull`` is real ``tenacity``; the
transient-error tests below patch ``tenacity.nap.sleep`` so retries do
not introduce wall time.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


NAME = "tolokasoft1/tolokaforge-runner"
TAG = "0.18.0"
FULL_TAG = f"{NAME}:{TAG}"


def _mock_client(pull_result: Any = None, get_side_effect: Any | None = None) -> MagicMock:
    """Build a MagicMock DockerClient wired for a pull test.

    ``get_side_effect`` controls what ``_find_existing_image`` sees.
    Default: raise ``ImageNotFound`` so the cache-hit branch is skipped
    and we always exercise the pull path.
    """
    docker = pytest.importorskip("docker")
    from docker.errors import ImageNotFound

    client = MagicMock(spec=docker.DockerClient)
    client.images = MagicMock()
    if get_side_effect is None:
        client.images.get.side_effect = ImageNotFound(f"no such image: {FULL_TAG}")
    else:
        client.images.get.side_effect = get_side_effect
    client.images.pull.return_value = pull_result
    return client


class TestImagePullHappyPath:
    def test_returns_image_with_pulled_sentinel_on_success(self) -> None:
        from tolokaforge.docker.image import Image

        pulled = MagicMock(name="pulled_docker_image")
        pulled.id = "sha256:abc123"
        client = _mock_client(pull_result=pulled)

        image = Image.pull(name=NAME, tag=TAG, client=client)

        assert image.name == NAME
        assert image.tag == TAG
        assert image.full_tag == FULL_TAG
        assert image.image_id == "sha256:abc123"
        assert image.context_hash == "pulled"
        assert image.dockerfile == "pulled"
        assert image.context == "pulled"

    def test_calls_pull_with_repository_and_tag_kwargs(self) -> None:
        from tolokaforge.docker.image import Image

        pulled = MagicMock()
        pulled.id = "sha256:xyz"
        client = _mock_client(pull_result=pulled)

        Image.pull(name=NAME, tag=TAG, client=client)

        client.images.pull.assert_called_once_with(NAME, tag=TAG)


class TestImagePullCacheHit:
    def test_skips_pull_when_local_daemon_already_has_image(self) -> None:
        from tolokaforge.docker.image import Image

        existing = MagicMock(name="existing_docker_image")
        existing.id = "sha256:already-here"

        # Cache hit — client.images.get returns the existing image.
        client = _mock_client(get_side_effect=None)
        client.images.get.side_effect = None
        client.images.get.return_value = existing

        image = Image.pull(name=NAME, tag=TAG, client=client)

        assert image.image_id == "sha256:already-here"
        client.images.pull.assert_not_called()


class TestImagePullErrors:
    def test_image_not_found_becomes_tag_missing(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import ImageNotFound

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        client.images.pull.side_effect = ImageNotFound(f"pull error: {FULL_TAG}")

        with pytest.raises(ImagePullError) as excinfo:
            Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "tag_missing"
        assert excinfo.value.full_tag == FULL_TAG
        _ = docker  # keep the importorskip in-scope

    def test_api_error_404_becomes_tag_missing(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import APIError

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        response = MagicMock()
        response.status_code = 404
        response.headers = {"X-Docker-Reason": "not found"}
        client.images.pull.side_effect = APIError("not found", response=response)

        with pytest.raises(ImagePullError) as excinfo:
            Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "tag_missing"
        _ = docker

    def test_api_error_429_becomes_rate_limited_and_carries_retry_after(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import APIError

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "60"}
        client.images.pull.side_effect = APIError("too many requests", response=response)

        with pytest.raises(ImagePullError) as excinfo:
            Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "rate_limited"
        assert excinfo.value.response_headers.get("Retry-After") == "60"
        # The message names auth as the fix — the actionable hint for a
        # rate-limited operator.
        assert "auth" in str(excinfo.value).lower()
        _ = docker

    def test_api_error_500_becomes_unreachable(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import APIError

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        response = MagicMock()
        response.status_code = 500
        response.headers = {}
        client.images.pull.side_effect = APIError("registry down", response=response)

        # Silence tenacity's sleep during transient-retry loops so a real
        # 500 test does not introduce wall time. We patch the module the
        # decorator's wait uses to schedule sleeps.
        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError) as excinfo:
                Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "unreachable"
        # Retried before failing — we should have called pull the full
        # 5-attempt budget for a transient error.
        assert client.images.pull.call_count == 5
        _ = docker

    def test_docker_exception_becomes_unreachable(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import DockerException

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        client.images.pull.side_effect = DockerException("daemon socket closed")

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError) as excinfo:
                Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "unreachable"
        _ = docker


class TestImagePullDoesNotRetryTerminal:
    def test_404_not_retried(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import APIError

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        response = MagicMock()
        response.status_code = 404
        response.headers = {}
        client.images.pull.side_effect = APIError("not found", response=response)

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError):
                Image.pull(name=NAME, tag=TAG, client=client)

        # 404 is terminal: exactly one attempt, no retries.
        assert client.images.pull.call_count == 1
        _ = docker

    def test_429_not_retried(self) -> None:
        docker = pytest.importorskip("docker")
        from docker.errors import APIError

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        response = MagicMock()
        response.status_code = 429
        response.headers = {}
        client.images.pull.side_effect = APIError("rate limit", response=response)

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError):
                Image.pull(name=NAME, tag=TAG, client=client)

        assert client.images.pull.call_count == 1
        _ = docker
