"""Tests for the single-sourced package version (tolokaforge.__version__)."""

import pytest

import tolokaforge

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestVersion:
    def test_version_is_non_empty_string(self):
        assert isinstance(tolokaforge.__version__, str)
        assert tolokaforge.__version__

    def test_version_matches_installed_metadata(self):
        """In an installed/editable env, __version__ mirrors the distribution
        metadata (the single source of truth in pyproject.toml). If the package
        is not installed, the documented fallback is used instead."""
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        try:
            expected = pkg_version("tolokaforge")
        except PackageNotFoundError:
            expected = "0.0.0+unknown"
        assert tolokaforge.__version__ == expected

    def test_version_exposed_in_all(self):
        assert "__version__" in tolokaforge.__all__
