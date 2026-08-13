"""Unit tests for the pull-vs-build branch inside ``EngineStack._build_one_image``.

Uses monkeypatched ``Image.pull`` and ``Image.build`` so no Docker
daemon is touched. Every test asserts the exact call shape (arguments,
call count) rather than just the return value — otherwise a change
that silently swaps pull for build (or vice versa) would still pass.

The pull-vs-build resolution wraps the pure :func:`resolve_image_source`
helper (tested exhaustively in ``test_image_source_policy.py``); this
file is about the CALLER, not the policy.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.image import Image, ImagePullError
from tolokaforge.docker.stack import EngineStack, ServiceDefinition

pytestmark = pytest.mark.unit


PUBLISHED_REPO = "tolokasoft1/tolokaforge-runner"


def _svc(
    *,
    published: str | None = PUBLISHED_REPO,
    use_prebuilt: bool = False,
    dockerfile: str = "docker/runner.Dockerfile",
) -> ServiceDefinition:
    """A minimal runnable ServiceDefinition for stack-branch tests."""
    return ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        published_image_repo=published,
        dockerfile=dockerfile if not use_prebuilt else "",
        context=".",
        use_prebuilt_image=use_prebuilt,
        prebuilt_tag="latest",
    )


def _fake_pulled_image() -> Image:
    return Image(
        name=PUBLISHED_REPO,
        tag="0.18.0",
        image_id="sha256:pulled-abc",
        dockerfile="pulled",
        context="pulled",
        context_hash="pulled",
    )


def _fake_built_image() -> Image:
    return Image(
        name="tolokaforge-runner",
        tag="deadbeef",
        image_id="sha256:built-def",
        dockerfile="docker/runner.Dockerfile",
        context=".",
        context_hash="a" * 64,
    )


class _StubRegistry:
    """Records calls to ``get_or_build`` — a stand-in for
    ``ImageRegistry.get_or_build`` that never touches Docker."""

    def __init__(self, ret: Image) -> None:
        self.calls: list[dict[str, Any]] = []
        self._ret = ret

    def get_or_build(self, **kwargs: Any) -> Image:
        self.calls.append(kwargs)
        return self._ret


def _make_stack(
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_source: str,
    pull_result: Image | Exception,
    is_wheel_install: bool,
    engine_version: str,
) -> tuple[EngineStack, MagicMock, _StubRegistry]:
    """Wire an EngineStack with the pull/build primitives stubbed.

    Returns (stack, pull_mock, build_registry).
    """
    stack = EngineStack(config=DockerConfig(image_source=image_source))  # type: ignore[arg-type]

    # Stub ``resolve_image_source``'s environment inputs — repo_root and
    # engine_version — inside the stack module namespace where
    # ``_maybe_pull_service_image`` looks them up.
    from tolokaforge.docker import builder as builder_module

    class _FakePath:
        def __init__(self, present: bool) -> None:
            self._present = present

        def __truediv__(self, _other: object) -> _FakePath:
            return self

        def is_file(self) -> bool:
            return self._present

    monkeypatch.setattr(
        builder_module,
        "repo_root",
        lambda: _FakePath(present=not is_wheel_install),
    )

    import tolokaforge as tolokaforge_pkg

    monkeypatch.setattr(tolokaforge_pkg, "__version__", engine_version)

    pull_mock = MagicMock(name="Image.pull")
    if isinstance(pull_result, Exception):
        pull_mock.side_effect = pull_result
    else:
        pull_mock.return_value = pull_result
    monkeypatch.setattr(Image, "pull", pull_mock)

    build_registry = _StubRegistry(_fake_built_image())
    stack._registry = build_registry  # type: ignore[assignment]

    return stack, pull_mock, build_registry


class TestAutoModeWheelInstall:
    def test_successful_pull_returns_pulled_image_and_skips_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        image = stack._build_one_image(_svc())

        assert image is not None
        assert image.context_hash == "pulled"
        # ``linux/amd64`` is the published-images platform axis; the caller
        # passes it explicitly so arm64 hosts can pull the amd64 variant.
        pull_mock.assert_called_once_with(name=PUBLISHED_REPO, tag="0.18.0", platform="linux/amd64")
        assert registry.calls == []  # build path never taken

    def test_tag_missing_fallback_to_build_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=ImagePullError("tag_missing", f"{PUBLISHED_REPO}:0.18.0", "not found"),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with caplog.at_level("WARNING", logger="tolokaforge.docker.stack"):
            image = stack._build_one_image(_svc())

        assert image is not None
        assert image.context_hash != "pulled"  # this is a built image
        pull_mock.assert_called_once()
        assert len(registry.calls) == 1
        # Warning names the failure kind + the falling-back action
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("tag_missing" in r.message for r in warnings), warnings
        assert any("Falling back to local build" in r.message for r in warnings), warnings

    def test_rate_limited_fallback_to_build_names_kind(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=ImagePullError(
                "rate_limited",
                f"{PUBLISHED_REPO}:0.18.0",
                "Docker Hub rate limit — configure auth",
                response_headers={"Retry-After": "60"},
            ),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with caplog.at_level("WARNING", logger="tolokaforge.docker.stack"):
            image = stack._build_one_image(_svc())

        assert image is not None
        pull_mock.assert_called_once()
        assert len(registry.calls) == 1
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("rate_limited" in r.message for r in warnings), warnings

    def test_unreachable_fallback_to_build(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stack, _pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=ImagePullError(
                "unreachable", f"{PUBLISHED_REPO}:0.18.0", "connection refused"
            ),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with caplog.at_level("WARNING", logger="tolokaforge.docker.stack"):
            image = stack._build_one_image(_svc())

        assert image is not None
        assert len(registry.calls) == 1


class TestAutoModeSourceCheckout:
    def test_source_checkout_builds_without_attempting_pull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=_fake_pulled_image(),
            is_wheel_install=False,
            engine_version="0.18.0",
        )

        stack._build_one_image(_svc())

        pull_mock.assert_not_called()
        assert len(registry.calls) == 1

    def test_unknown_version_builds_without_attempting_pull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, _registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.0.0+unknown",
        )

        stack._build_one_image(_svc())

        pull_mock.assert_not_called()


class TestExplicitPullMode:
    def test_pull_success_returns_pulled_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="pull",
            pull_result=_fake_pulled_image(),
            # Explicit pull mode overrides install shape — pull even on a
            # source checkout.
            is_wheel_install=False,
            engine_version="0.18.0",
        )

        image = stack._build_one_image(_svc())

        assert image is not None and image.context_hash == "pulled"
        pull_mock.assert_called_once()
        assert registry.calls == []

    def test_pull_failure_is_hard_error_no_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="pull",
            pull_result=ImagePullError("tag_missing", f"{PUBLISHED_REPO}:0.18.0", "not found"),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with pytest.raises(ImagePullError) as excinfo:
            stack._build_one_image(_svc())

        assert excinfo.value.kind == "tag_missing"
        pull_mock.assert_called_once()
        # Explicit mode never falls back — build path is untouched.
        assert registry.calls == []


class TestExplicitBuildMode:
    def test_build_mode_never_calls_pull_even_on_wheel_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="build",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        stack._build_one_image(_svc())

        pull_mock.assert_not_called()
        assert len(registry.calls) == 1


class TestPrebuiltImageBranchIsUnchanged:
    def test_use_prebuilt_image_short_circuits_before_pull_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="pull",  # even in explicit pull mode
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        svc = _svc(use_prebuilt=True, published=None)

        image = stack._build_one_image(svc)

        assert image is not None
        assert image.context_hash == "prebuilt"
        pull_mock.assert_not_called()
        assert registry.calls == []


class TestServiceWithoutPublishedRepoAlwaysBuilds:
    def test_no_published_repo_falls_through_to_build_in_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )
        svc = _svc(published=None)

        stack._build_one_image(svc)

        pull_mock.assert_not_called()
        assert len(registry.calls) == 1


class TestExplicitPullModeContract:
    """Two contract corners the sibling ``TestExplicitPullMode`` does
    not cover: explicit ``pull`` mode refuses to fall back to build
    silently when the request cannot be honoured."""

    def test_force_true_in_explicit_pull_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``force=True`` normally skips the pull path (fresh rebuild),
        but in explicit ``pull`` mode that would silently violate the
        pull-or-die contract."""
        stack, _pull_mock, _registry = _make_stack(
            monkeypatch,
            image_source="pull",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with pytest.raises(ValueError) as excinfo:
            stack._build_one_image(_svc(), force=True)

        assert "pull" in str(excinfo.value).lower()
        assert "force" in str(excinfo.value).lower()

    def test_missing_published_repo_in_explicit_pull_mode_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A service without ``published_image_repo`` has nothing to
        pull from; in explicit ``pull`` mode that's a hard configuration
        error rather than a silent drop to build."""
        stack, _pull_mock, _registry = _make_stack(
            monkeypatch,
            image_source="pull",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        with pytest.raises(ValueError) as excinfo:
            stack._build_one_image(_svc(published=None))

        assert "published_image_repo" in str(excinfo.value)


class TestForceRebuildSkipsPull:
    def test_force_true_skips_pull_and_calls_image_build_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, pull_mock, registry = _make_stack(
            monkeypatch,
            image_source="auto",
            pull_result=_fake_pulled_image(),
            is_wheel_install=True,
            engine_version="0.18.0",
        )

        # Also stub Image.build so force-path testing doesn't need a
        # real dockerfile on disk.
        build_mock = MagicMock(name="Image.build", return_value=_fake_built_image())
        monkeypatch.setattr(Image, "build", build_mock)

        stack._build_one_image(_svc(), force=True)

        pull_mock.assert_not_called()
        build_mock.assert_called_once()
        # ``force=True`` calls Image.build directly, bypassing the
        # registry cache.
        assert registry.calls == []
