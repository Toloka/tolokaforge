"""The harness image layer is named by its bytes, not by its parameters.

An image ref keyed on the harness name and its pinned CLI version cannot
tell two builds of different bytes apart, so a caller that reuses an image
because its ref resolves runs the previous build. These cases pin the
property that makes the reuse safe: the digest moves when the baked-in
content moves, and only then.
"""

from pathlib import Path

import pytest
from tolokaforge_coding_harnesses.image_identity import (
    DIGEST_LENGTH,
    harness_image_content_digest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def context(tmp_path: Path) -> Path:
    harness = tmp_path / "_harness"
    harness.mkdir()
    (harness / "harness.Dockerfile").write_text("FROM base:local\n")
    (harness / "install-harness.sh").write_text("#!/bin/sh\nexit 0\n")
    return tmp_path


def _digest(context: Path) -> str:
    return harness_image_content_digest({"_harness": context / "_harness"})


class TestDigestShape:
    def test_digest_is_lowercase_hex_of_the_tag_length(self, context: Path) -> None:
        """It lands in a Docker tag, whose charset is ``[A-Za-z0-9_.-]``."""
        digest = _digest(context)
        assert len(digest) == DIGEST_LENGTH
        assert all(c in "0123456789abcdef" for c in digest)

    def test_a_file_part_and_a_directory_part_both_contribute(self, tmp_path: Path) -> None:
        loose = tmp_path / ".dockerignore"
        loose.write_text("*\n")
        tree = tmp_path / "skills"
        tree.mkdir()
        (tree / "SKILL.md").write_text("a\n")

        both = harness_image_content_digest({".dockerignore": loose, "skills": tree})
        assert both != harness_image_content_digest({".dockerignore": loose})
        assert both != harness_image_content_digest({"skills": tree})


class TestDigestMovesWithContent:
    def test_editing_a_baked_in_file_moves_the_digest(self, context: Path) -> None:
        before = _digest(context)
        script = context / "_harness" / "install-harness.sh"
        script.write_text(script.read_text() + "# one more line\n")
        assert _digest(context) != before

    def test_adding_a_baked_in_file_moves_the_digest(self, context: Path) -> None:
        before = _digest(context)
        (context / "_harness" / "middleware_proxy.py").write_text("pass\n")
        assert _digest(context) != before

    def test_renaming_a_baked_in_file_moves_the_digest(self, context: Path) -> None:
        """Paths are hashed alongside bytes: where a file lands in the build
        context decides whether the Dockerfile's ``COPY`` finds it."""
        before = _digest(context)
        script = context / "_harness" / "install-harness.sh"
        script.rename(script.with_name("install.sh"))
        assert _digest(context) != before

    def test_the_label_a_part_carries_is_part_of_the_digest(self, context: Path) -> None:
        under_one_label = harness_image_content_digest({"_harness": context / "_harness"})
        under_another = harness_image_content_digest({"other": context / "_harness"})
        assert under_one_label != under_another


class TestDigestStaysPut:
    def test_the_same_bytes_give_the_same_digest(self, context: Path) -> None:
        assert _digest(context) == _digest(context)

    def test_the_digest_is_independent_of_where_the_context_lives(self, tmp_path: Path) -> None:
        """Absolute paths must not enter it, or the canonical snapshots holding
        an image ref would differ per machine."""
        digests = set()
        for name in ("a", "b"):
            harness = tmp_path / name / "_harness"
            harness.mkdir(parents=True)
            (harness / "harness.Dockerfile").write_text("FROM base:local\n")
            digests.add(harness_image_content_digest({"_harness": harness}))
        assert len(digests) == 1

    def test_cache_noise_does_not_move_the_digest(self, context: Path) -> None:
        before = _digest(context)
        cache = context / "_harness" / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"\x00")
        (context / "_harness" / "build.log").write_text("noise\n")
        assert _digest(context) == before


class TestMissingPartIsLoud:
    def test_a_part_that_does_not_exist_raises(self, tmp_path: Path) -> None:
        """Every part is something the caller just wrote, so a missing one
        means the digest does not describe the context that was built."""
        with pytest.raises(FileNotFoundError, match="_harness"):
            harness_image_content_digest({"_harness": tmp_path / "_harness"})
