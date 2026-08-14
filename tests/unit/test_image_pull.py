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

    def test_platform_kwarg_is_forwarded_when_set(self) -> None:
        """Published tolokaforge-* images are linux/amd64 only. On an
        arm64 host a bare ``docker pull`` fails with 'no matching
        manifest for linux/arm64'; the caller passes an explicit
        platform so both amd64 hosts (native) and arm64 hosts (under
        emulation) land the same amd64 image."""
        from tolokaforge.docker.image import Image

        pulled = MagicMock()
        pulled.id = "sha256:xyz"
        client = _mock_client(pull_result=pulled)

        Image.pull(name=NAME, tag=TAG, platform="linux/amd64", client=client)

        client.images.pull.assert_called_once_with(NAME, tag=TAG, platform="linux/amd64")


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

    def test_cache_hit_verifies_platform_arch_and_re_pulls_on_mismatch(self) -> None:
        """If a cached image is present but its Os/Architecture do not
        match the requested platform (e.g. an operator previously
        ``docker tag``-ed an arm64 image onto the pulled tag), the pull
        must NOT short-circuit — otherwise the caller gets a
        wrong-arch image that fails much later with 'exec format error'."""
        from tolokaforge.docker.image import Image

        cached_wrong_arch = MagicMock(name="cached_wrong_arch")
        cached_wrong_arch.id = "sha256:arm64-copy"
        cached_wrong_arch.attrs = {"Os": "linux", "Architecture": "arm64"}

        pulled_correct = MagicMock(name="pulled_correct")
        pulled_correct.id = "sha256:amd64-fresh"

        client = _mock_client(get_side_effect=None)
        client.images.get.side_effect = None
        client.images.get.return_value = cached_wrong_arch
        client.images.pull.return_value = pulled_correct

        image = Image.pull(name=NAME, tag=TAG, platform="linux/amd64", client=client)

        # Cache short-circuit was skipped — a fresh pull actually
        # happened.
        client.images.pull.assert_called_once_with(NAME, tag=TAG, platform="linux/amd64")
        assert image.image_id == "sha256:amd64-fresh"

    def test_cache_hit_uses_cached_image_when_platform_matches(self) -> None:
        from tolokaforge.docker.image import Image

        cached_correct = MagicMock(name="cached_correct")
        cached_correct.id = "sha256:amd64-cached"
        cached_correct.attrs = {"Os": "linux", "Architecture": "amd64"}

        client = _mock_client(get_side_effect=None)
        client.images.get.side_effect = None
        client.images.get.return_value = cached_correct

        image = Image.pull(name=NAME, tag=TAG, platform="linux/amd64", client=client)

        assert image.image_id == "sha256:amd64-cached"
        client.images.pull.assert_not_called()

    def test_cache_hit_re_pulls_when_attrs_missing(self) -> None:
        """When the cached image's attrs don't expose Os/Architecture
        (unexpected daemon behavior, older docker version, etc.), err
        on the side of re-pulling rather than returning an unverifiable
        image."""
        from tolokaforge.docker.image import Image

        cached_unknown = MagicMock(name="cached_unknown")
        cached_unknown.id = "sha256:unknown"
        cached_unknown.attrs = {}

        pulled = MagicMock()
        pulled.id = "sha256:fresh"

        client = _mock_client(get_side_effect=None)
        client.images.get.side_effect = None
        client.images.get.return_value = cached_unknown
        client.images.pull.return_value = pulled

        Image.pull(name=NAME, tag=TAG, platform="linux/amd64", client=client)

        client.images.pull.assert_called_once()


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

    def test_docker_exception_becomes_unreachable_without_retries(self) -> None:
        """Bare ``DockerException`` (e.g. 'daemon socket closed') is
        NOT retried — it isn't recoverable by waiting, and burning ~130 s
        of tenacity backoff on it just delays surfacing the real
        problem. Only network-layer transient errors get the full
        5-attempt budget (see the 500 test above)."""
        docker = pytest.importorskip("docker")
        from docker.errors import DockerException

        from tolokaforge.docker.image import Image, ImagePullError

        client = _mock_client()
        client.images.pull.side_effect = DockerException("daemon socket closed")

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError) as excinfo:
                Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "unreachable"
        # Bare DockerException is terminal — no retries.
        assert client.images.pull.call_count == 1
        _ = docker

    def test_connection_error_becomes_unreachable_after_retries(self) -> None:
        """``requests.ConnectionError`` and other ``OSError`` subclasses
        can escape docker-py un-wrapped when the daemon socket becomes
        unresponsive mid-pull. Classify as transient and burn the full
        retry budget before surfacing — the daemon might come back."""
        client = _mock_client()
        client.images.pull.side_effect = ConnectionError("daemon socket flapping")

        from tolokaforge.docker.image import Image, ImagePullError

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError) as excinfo:
                Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "unreachable"
        assert client.images.pull.call_count == 5

    def test_os_error_becomes_unreachable_after_retries(self) -> None:
        """Raw ``OSError`` (broken socket, EPIPE, etc.) — same shape."""
        client = _mock_client()
        client.images.pull.side_effect = OSError("EPIPE")

        from tolokaforge.docker.image import Image, ImagePullError

        with patch("tenacity.nap.time.sleep"):
            with pytest.raises(ImagePullError) as excinfo:
                Image.pull(name=NAME, tag=TAG, client=client)

        assert excinfo.value.kind == "unreachable"
        assert client.images.pull.call_count == 5


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
