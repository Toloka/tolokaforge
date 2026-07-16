"""Unit tests for ``tolokaforge assets stamp`` and the digest helper.

The verb walks ``assets.seeds.<name>`` entries, computes
``sha256:<hex>`` over each referenced seed — a file's byte stream or a
directory's tree hash — and writes the ``digest`` field back into
``project.yaml``. This suite covers write mode, ``--check`` dry-run,
missing-file failure, bare-string shorthand coercion, directory
(``filesystem_dir``) seeds, and idempotency.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.cli.main import cli
from tolokaforge.core.assets import compute_seed_digest

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _write_project(
    tmp_path: Path,
    seed_content: bytes = b"-- baseline\n",
    *,
    seeds_block: dict | None = None,
) -> tuple[Path, Path]:
    """Write a minimal project.yaml + seed file. Returns
    ``(project_yaml_path, seed_path)``. The default seed block uses
    the dict form with a placeholder digest; callers pass
    ``seeds_block`` when they need the bare-string shorthand or a
    pre-stamped digest.
    """
    seed_path = tmp_path / "shared" / "seeds" / "base.sql"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_bytes(seed_content)

    if seeds_block is None:
        seeds_block = {
            "base": {
                "path": "shared/seeds/base.sql",
                "kind": "sql_dump",
                "digest": "sha256:placeholder",
            },
        }

    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(
        yaml.safe_dump(
            {
                "name": "p",
                "assets": {"seeds": seeds_block},
            },
            sort_keys=False,
        ),
    )
    return project_yaml, seed_path


class TestComputeSeedDigest:
    def test_produces_sha256_prefixed_hex(self, tmp_path: Path) -> None:
        # Compare against a hashlib-computed reference so the format
        # ("sha256:<64-hex>") is pinned end-to-end.
        seed = tmp_path / "seed.sql"
        seed.write_bytes(b"hello, seed\n")
        expected = "sha256:" + hashlib.sha256(b"hello, seed\n").hexdigest()
        assert compute_seed_digest(seed) == expected

    def test_streams_large_files(self, tmp_path: Path) -> None:
        # 128 KiB — larger than the 64 KiB block size — exercises
        # multi-block streaming and confirms the digest matches a
        # whole-file hash. Regression net for a future rewrite that
        # tries to read-all.
        seed = tmp_path / "large.rdb"
        payload = b"x" * (128 * 1024)
        seed.write_bytes(payload)
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert compute_seed_digest(seed) == expected

    def test_block_boundary_edge_case(self, tmp_path: Path) -> None:
        # Non-multiple-of-block-size payloads exercise the "final
        # chunk shorter than block size" branch of the streaming
        # loop. 65 KiB = one full 64 KiB block + one 1 KiB tail —
        # pins that the tail read is included in the hash.
        seed = tmp_path / "boundary.sql"
        payload = b"a" * (65 * 1024)
        seed.write_bytes(payload)
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert compute_seed_digest(seed) == expected

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            compute_seed_digest(tmp_path / "does-not-exist.sql")


class TestAssetsStampWriteMode:
    def test_fresh_digest_written_and_idempotent(self, runner: CliRunner, tmp_path: Path) -> None:
        project_yaml, seed = _write_project(tmp_path)
        expected = compute_seed_digest(seed)

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "wrote 1 digest" in result.output

        rewritten = yaml.safe_load(project_yaml.read_text())
        assert rewritten["assets"]["seeds"]["base"]["digest"] == expected

        # Idempotent: re-running against the now-stamped file is a
        # no-op that says so and doesn't touch the file. Rich is
        # configured with ``soft_wrap=True`` so the phrase stays on
        # one line even in narrow CI terminals.
        mtime_after_first = project_yaml.stat().st_mtime_ns
        result2 = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result2.exit_code == 0
        assert "already current" in result2.output
        assert project_yaml.stat().st_mtime_ns == mtime_after_first

    def test_directory_seed_stamped_and_check_passes(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # A ``filesystem_dir`` seed's ``path`` is a directory tree, not a
        # file. Stamp must digest the tree (not reject it as "missing") and
        # ``--check`` must then confirm the written digest matches.
        seed_dir = tmp_path / "assets" / "source"
        (seed_dir / "pkg").mkdir(parents=True)
        (seed_dir / "app.py").write_text("x = 1\n")
        (seed_dir / "pkg" / "mod.py").write_text("y = 2\n")
        expected = compute_seed_digest(seed_dir)

        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "name": "p",
                    "assets": {
                        "seeds": {
                            "src": {
                                "path": "assets/source",
                                "kind": "filesystem_dir",
                                "digest": "sha256:placeholder",
                            },
                        },
                    },
                },
                sort_keys=False,
            ),
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "wrote 1 digest" in result.output
        entry = yaml.safe_load(project_yaml.read_text())["assets"]["seeds"]["src"]
        assert entry["digest"] == expected

        check = runner.invoke(cli, ["assets", "stamp", "--check", str(tmp_path)])
        assert check.exit_code == 0, check.output
        assert "match" in check.output

    def test_bare_string_shorthand_coerced_to_dict_with_digest(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Bare-string shorthand can't carry a digest; the write path
        # must coerce it into dict form with the digest attached.
        project_yaml, seed = _write_project(
            tmp_path,
            seeds_block={"base": "shared/seeds/base.sql"},
        )
        expected = compute_seed_digest(seed)

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output

        entry = yaml.safe_load(project_yaml.read_text())["assets"]["seeds"]["base"]
        assert isinstance(entry, dict)
        assert entry["path"] == "shared/seeds/base.sql"
        assert entry["kind"] == "sql_dump"
        assert entry["digest"] == expected

    def test_multiple_seeds_only_stale_entries_rewritten(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Two-seed project: one already-current, one stale. The stamp
        # must update only the stale entry and leave the current one
        # untouched byte-for-byte (in the sense that the digest doesn't
        # get re-computed to something different). Also exercises the
        # ``changed = True`` accumulation across entries.
        (tmp_path / "shared" / "seeds").mkdir(parents=True)
        (tmp_path / "shared" / "seeds" / "a.sql").write_bytes(b"-- a\n")
        (tmp_path / "shared" / "seeds" / "b.sql").write_bytes(b"-- b\n")
        digest_a = compute_seed_digest(tmp_path / "shared" / "seeds" / "a.sql")
        digest_b = compute_seed_digest(tmp_path / "shared" / "seeds" / "b.sql")
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "name": "p",
                    "assets": {
                        "seeds": {
                            # ``a`` is already stamped correctly.
                            "a": {
                                "path": "shared/seeds/a.sql",
                                "kind": "sql_dump",
                                "digest": digest_a,
                            },
                            # ``b`` has a placeholder digest — needs update.
                            "b": {
                                "path": "shared/seeds/b.sql",
                                "kind": "sql_dump",
                                "digest": "sha256:placeholder",
                            },
                        },
                    },
                },
                sort_keys=False,
            ),
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output
        # One digest changed, one already correct → wrote 1.
        assert "wrote 1 digest" in result.output

        rewritten = yaml.safe_load(project_yaml.read_text())["assets"]["seeds"]
        assert rewritten["a"]["digest"] == digest_a  # unchanged
        assert rewritten["b"]["digest"] == digest_b  # updated

    def test_explicit_project_yaml_file_argument(self, runner: CliRunner, tmp_path: Path) -> None:
        # PROJECT_PATH may point directly at a project.yaml file (not
        # just its containing directory). Pins the file-form branch of
        # ``_resolve_project_yaml``.
        project_yaml, seed = _write_project(tmp_path)
        expected = compute_seed_digest(seed)

        result = runner.invoke(cli, ["assets", "stamp", str(project_yaml)])
        assert result.exit_code == 0, result.output

        entry = yaml.safe_load(project_yaml.read_text())["assets"]["seeds"]["base"]
        assert entry["digest"] == expected

    def test_relative_path_stays_relative_on_write(self, runner: CliRunner, tmp_path: Path) -> None:
        # The on-disk path must remain project-relative — rewriting to
        # absolute would break portability across checkouts.
        project_yaml, _ = _write_project(tmp_path)

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0

        entry = yaml.safe_load(project_yaml.read_text())["assets"]["seeds"]["base"]
        assert entry["path"] == "shared/seeds/base.sql"


class TestAssetsStampCheckMode:
    def test_check_matches_exits_zero_no_write(self, runner: CliRunner, tmp_path: Path) -> None:
        project_yaml, seed = _write_project(tmp_path)
        # First, write the current digest so --check sees no drift.
        runner.invoke(cli, ["assets", "stamp", str(tmp_path)])

        mtime_before = project_yaml.stat().st_mtime_ns
        result = runner.invoke(cli, ["assets", "stamp", "--check", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "match" in result.output
        assert project_yaml.stat().st_mtime_ns == mtime_before

    def test_check_stale_digest_exits_one_with_diff(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        project_yaml, seed = _write_project(tmp_path)  # placeholder digest
        expected_new = compute_seed_digest(seed)

        result = runner.invoke(cli, ["assets", "stamp", "--check", str(tmp_path)])
        assert result.exit_code != 0
        assert "stale" in result.output
        # Both the OLD (placeholder) and the computed NEW digest must
        # appear in the diff, structured as ``old → new``. A regression
        # to only-print-one would slip past a looser assertion.
        assert f"sha256:placeholder → {expected_new}" in result.output


class TestAssetsStampMissingFile:
    def test_missing_seed_path_exits_non_zero_with_named_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Author-typo case: project.yaml references a file that isn't
        # on disk. Must fail loud naming the path so the author fixes
        # the reference rather than debugging a mystery hash mismatch.
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "name": "p",
                    "assets": {
                        "seeds": {
                            "base": {
                                "path": "shared/seeds/missing.sql",
                                "kind": "sql_dump",
                            },
                        },
                    },
                },
                sort_keys=False,
            ),
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code != 0
        # Errors route through ``err_console`` (stderr) — uniform with
        # every other fatal-error path in the module. The offending
        # path must appear on stderr so the author knows exactly which
        # reference to fix.
        assert "shared/seeds/missing.sql" in result.stderr
        assert "does not exist" in result.stderr


class TestAssetsStampCheckWithMissingFile:
    def test_check_mode_fails_when_seed_file_missing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # --check must NOT mask a missing seed file — the whole point of
        # dry-run in CI is catching drift before it lands. Order of the
        # exit paths (missing file check before check-only branch) is
        # what makes this work; pinning it here so a future reorder
        # can't silently break the guarantee.
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "name": "p",
                    "assets": {
                        "seeds": {
                            "base": {
                                "path": "shared/seeds/missing.sql",
                                "kind": "sql_dump",
                            },
                        },
                    },
                },
                sort_keys=False,
            ),
        )

        result = runner.invoke(cli, ["assets", "stamp", "--check", str(tmp_path)])
        assert result.exit_code != 0
        # Fatal errors route to stderr (see err_console in
        # assets_commands.py).
        assert "shared/seeds/missing.sql" in result.stderr


class TestAssetsStampProjectResolution:
    def test_walks_up_from_directory_argument(self, runner: CliRunner, tmp_path: Path) -> None:
        # Passing a subdirectory finds the enclosing project.yaml via
        # find_project_yaml. Mirrors the CWD-default behaviour without
        # depending on the invoker's current directory.
        _write_project(tmp_path)
        nested = tmp_path / "nested" / "deep"
        nested.mkdir(parents=True)

        result = runner.invoke(cli, ["assets", "stamp", str(nested)])
        assert result.exit_code == 0, result.output

    def test_no_project_yaml_exits_non_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        # Empty directory — no project.yaml at or above. Fails loud.
        empty = tmp_path / "empty"
        empty.mkdir()

        result = runner.invoke(cli, ["assets", "stamp", str(empty)])
        assert result.exit_code != 0
        # Fatal errors route to stderr.
        assert "No project.yaml" in result.stderr


class TestAssetsStampMalformedShapeFailsLoud:
    """Present-but-wrong shapes on ``assets`` or ``assets.seeds``
    must NOT report ``nothing to stamp`` — a ``--check`` in CI
    should fail so a typo doesn't ride green through the pipeline.
    Absent vs. present-but-broken is the important distinction."""

    def test_assets_not_a_mapping_fails_loud(self, runner: CliRunner, tmp_path: Path) -> None:
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(yaml.safe_dump({"name": "p", "assets": "oops"}))

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code != 0
        # ``click.ClickException`` writes to stderr, and the fixture
        # is ``mix_stderr=False`` — check the split stream.
        assert "assets" in result.stderr
        assert "must be a mapping" in result.stderr

    def test_seeds_not_a_mapping_fails_loud(self, runner: CliRunner, tmp_path: Path) -> None:
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump({"name": "p", "assets": {"seeds": "oops"}}),
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code != 0
        assert "assets.seeds" in result.stderr
        assert "must be a mapping" in result.stderr

    def test_check_mode_also_fails_loud_on_malformed(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # The whole point of failing loud on malformed shapes is that
        # --check in CI catches them. Pin that explicitly.
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump({"name": "p", "assets": {"seeds": "oops"}}),
        )

        result = runner.invoke(cli, ["assets", "stamp", "--check", str(tmp_path)])
        assert result.exit_code != 0


class TestAssetsStampCommentDetection:
    """The comment-loss warning must fire for both whole-line and
    inline comment shapes; PyYAML strips both."""

    def test_inline_comment_triggers_warning_on_write(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Hand-author the project.yaml (yaml.safe_dump produces no
        # comments, so we have to write raw). Inline comment on the
        # digest line is a common author pattern.
        seed = tmp_path / "shared" / "seeds" / "base.sql"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"-- baseline\n")
        (tmp_path / "project.yaml").write_text(
            "name: p\n"
            "assets:\n"
            "  seeds:\n"
            "    base:\n"
            "      path: shared/seeds/base.sql  # inline note about the seed\n"
            "      kind: sql_dump\n"
            "      digest: sha256:placeholder\n",
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "strip on write" in result.output

    def test_whole_line_comment_triggers_warning(self, runner: CliRunner, tmp_path: Path) -> None:
        seed = tmp_path / "shared" / "seeds" / "base.sql"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"-- baseline\n")
        (tmp_path / "project.yaml").write_text(
            "# Full-line comment at top\n"
            "name: p\n"
            "assets:\n"
            "  seeds:\n"
            "    base:\n"
            "      path: shared/seeds/base.sql\n"
            "      kind: sql_dump\n"
            "      digest: sha256:placeholder\n",
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "strip on write" in result.output


class TestAssetsStampNoSeeds:
    def test_project_without_assets_block_no_op(self, runner: CliRunner, tmp_path: Path) -> None:
        # A project.yaml with no `assets` block is legal today; stamp
        # is a no-op that says so rather than raising.
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(yaml.safe_dump({"name": "p"}))

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])
        assert result.exit_code == 0
        assert "nothing to stamp" in result.output


class TestAssetsStampHelpText:
    def test_group_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["assets", "--help"])
        assert result.exit_code == 0
        assert "stamp" in result.output

    def test_stamp_help_documents_check_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["assets", "stamp", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
