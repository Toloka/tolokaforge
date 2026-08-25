"""Snapshot the agent-visible dir of a harness-mode trial container.

Locks the contract of :func:`snapshot_container_filesystem`:

* the ``tar | base64`` payload the exec-wrapper returns decodes into the
  ``{container_path: text}`` shape ``StateChecker.check_jsonpaths`` reads;
* per-file and per-run caps drop oversized files without failing the snapshot;
* an absent agent-visible dir is a silent skip, not a grading error;
* binary files and symlinks are omitted (the assertion vocabulary is text-only).
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile

import pytest

from tolokaforge.runner.harness_state import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    snapshot_container_filesystem,
)

pytestmark = pytest.mark.unit


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Build a POSIX tarball with the given ``{./path: bytes}`` files.

    Mirrors the exact shape ``tar -cf - .`` produces inside the container —
    member names begin with ``./`` and are relative to the working directory
    of the exec call. Empty archives are legal.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(name=name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return buf.getvalue()


def _b64(data: bytes) -> str:
    """Base64-encode ``data`` the way ``| base64`` inside the container does.

    Plain :mod:`base64` is close enough — the receiver's decoder is lenient
    about the wrap boundaries the two implementations disagree on.
    """
    return base64.b64encode(data).decode("ascii")


class _ScriptedExec:
    """Stub for ``DockerComposeExecToolWrapper._exec_sync``.

    Sequenced return values, one per call, and records each command for later
    assertions. Extra calls raise so an over-eager helper trips a test.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, command: str, timeout: float) -> str:
        self.calls.append((command, timeout))
        if not self._responses:
            raise AssertionError(f"unexpected exec call: {command!r}")
        return self._responses.pop(0)


class TestSnapshotHappyPath:
    def test_returns_files_keyed_under_agent_visible_dir(self) -> None:
        tar = _make_tarball(
            {
                "./factorial.py": b"def factorial(n): return 1\n",
                "./tests/test_factorial.py": b"assert factorial(1) == 1\n",
            }
        )
        exec_fn = _ScriptedExec(
            [
                "1024\t/work\n",  # du output
                _b64(tar),
            ]
        )

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {
            "/work/factorial.py": "def factorial(n): return 1\n",
            "/work/tests/test_factorial.py": "assert factorial(1) == 1\n",
        }

    def test_du_probe_precedes_tar_read(self) -> None:
        # The probe runs first so an oversized tree is refused before the
        # (potentially large) tarball flows over the exec channel.
        tar = _make_tarball({"./x.py": b"y = 1\n"})
        exec_fn = _ScriptedExec(["512\t/work\n", _b64(tar)])

        snapshot_container_filesystem(exec_fn, "/work")

        assert len(exec_fn.calls) == 2
        assert "du -sb" in exec_fn.calls[0][0]
        assert "tar --exclude=./.git" in exec_fn.calls[1][0]
        assert "base64" in exec_fn.calls[1][0]

    def test_agent_visible_dir_is_shell_quoted(self) -> None:
        # A dir with a space must survive the shell round-trip intact.
        tar = _make_tarball({"./ok": b"ok"})
        exec_fn = _ScriptedExec(["4\tdir\n", _b64(tar)])

        snapshot_container_filesystem(exec_fn, "/work with space")

        for cmd, _ in exec_fn.calls:
            assert "'/work with space'" in cmd


class TestSnapshotAbortsCleanly:
    def test_missing_agent_visible_dir_returns_empty(self) -> None:
        # The container answer for an absent dir is `MISSING`; the helper
        # returns {} rather than crashing the grade or reading /.
        exec_fn = _ScriptedExec(["MISSING\n"])

        fs = snapshot_container_filesystem(exec_fn, "/nope")

        assert fs == {}
        assert len(exec_fn.calls) == 1  # tar step never issued

    def test_tree_over_total_cap_is_skipped(self) -> None:
        # A tree du reports over the cap returns {} before any tar read.
        exec_fn = _ScriptedExec([f"{DEFAULT_MAX_TOTAL_BYTES + 1}\t/work\n"])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {}
        assert len(exec_fn.calls) == 1

    def test_malformed_tarball_returns_empty(self) -> None:
        # A tar step whose payload is not a tarball fails soft: {} instead of
        # raising, so grading can still report on transcript / judge components.
        exec_fn = _ScriptedExec(["10\t/work\n", _b64(b"not a tarball")])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {}

    def test_du_unparseable_output_still_attempts_tar(self) -> None:
        # A busybox du variant reporting KB blocks (or otherwise unparseable
        # output) does NOT block the snapshot — the tar exec is separately
        # budgeted, so the worst case is a slow-but-honest read.
        tar = _make_tarball({"./x": b"ok"})
        exec_fn = _ScriptedExec(["not-a-number\n", _b64(tar)])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {"/work/x": "ok"}


class TestPerFileCap:
    def test_oversized_file_is_skipped_others_kept(self) -> None:
        big = b"x" * (DEFAULT_MAX_FILE_BYTES + 1)
        small = b"y" * 32
        tar = _make_tarball({"./big.bin": big, "./small.py": small})
        exec_fn = _ScriptedExec([f"{DEFAULT_MAX_FILE_BYTES + 1000}\t/work\n", _b64(tar)])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert "/work/big.bin" not in fs
        assert fs["/work/small.py"] == "y" * 32

    def test_custom_caps_are_honoured(self) -> None:
        # Ten bytes in, cap set to 5 — the file must drop.
        tar = _make_tarball({"./over.txt": b"0123456789"})
        exec_fn = _ScriptedExec(["10\t/work\n", _b64(tar)])

        fs = snapshot_container_filesystem(exec_fn, "/work", max_file_bytes=5)

        assert fs == {}


class TestBinaryAndSymlinkSkip:
    def test_non_utf8_file_is_skipped(self) -> None:
        tar = _make_tarball(
            {
                "./image.bin": b"\x89PNG\x00\x01\x02\x03\xff\xfe",
                "./readme.txt": b"hello\n",
            }
        )
        exec_fn = _ScriptedExec(["16\t/work\n", _b64(tar)])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {"/work/readme.txt": "hello\n"}

    def test_symlink_member_is_skipped(self) -> None:
        # A symlink member could resolve to any path the container process can
        # read; the assertion vocabulary was not designed to expose that.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            link = tarfile.TarInfo(name="./link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/hostname"
            tar.addfile(link)
            real = tarfile.TarInfo(name="./real.txt")
            real.size = 3
            tar.addfile(real, io.BytesIO(b"ok\n"))
        exec_fn = _ScriptedExec(["4\t/work\n", _b64(buf.getvalue())])

        fs = snapshot_container_filesystem(exec_fn, "/work")

        assert fs == {"/work/real.txt": "ok\n"}


class TestLoggingSurface:
    def test_cap_hit_is_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        exec_fn = _ScriptedExec([f"{DEFAULT_MAX_TOTAL_BYTES + 1}\t/work\n"])

        with caplog.at_level(logging.WARNING):
            snapshot_container_filesystem(exec_fn, "/work")

        assert any("tree exceeds total cap" in rec.message for rec in caplog.records)

    def test_missing_dir_is_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        exec_fn = _ScriptedExec(["MISSING\n"])

        with caplog.at_level(logging.WARNING):
            snapshot_container_filesystem(exec_fn, "/nope")

        assert any("agent-visible dir not found" in rec.message for rec in caplog.records)
