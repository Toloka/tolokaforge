"""What ``install-harness.sh`` does with each ``install_method``.

Drives the real script under ``bash`` with the network calls stubbed, so the
four-way install dispatch is exercised rather than described: an unrecognised
harness aborts, a URL method refuses ``version: "latest"``, and every method
records what it installed.
"""

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestInstallHarnessScript:
    """The install script is the only place a harness's install steps live."""

    _DEFAULT_DOWNLOAD = 'echo "installer $*" >> {record}\n'

    @staticmethod
    def _run_script(tmp_path: Path, *args: str, download: Path | None = None):
        """Run the script with fake package managers on ``PATH``.

        Each fake appends its argv to one record file, so a dispatch assertion
        reads as the request the method made of its tool — behavioural rather
        than source-scraping. ``curl`` additionally copies *download* to the
        path behind ``-o``, standing in for what the URL would have served.
        """
        from tolokaforge_coding_harnesses import INSTALL_SCRIPT

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        record = tmp_path / "tool-argv.txt"
        if download is None:
            download = tmp_path / "downloaded"
            download.write_text(TestInstallHarnessScript._DEFAULT_DOWNLOAD.format(record=record))

        def _fake(name: str, body: str = "") -> None:
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\necho "$@" >> {record}\n{body}')
            path.chmod(0o755)

        _fake("npm")
        _fake("pip")
        _fake(
            "curl",
            'out=""\nprev=""\nfor arg in "$@"; do\n'
            '  if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
            '  prev="$arg"\ndone\n'
            f'if [ -n "$out" ]; then cp {download} "$out"; fi\n',
        )
        # ``install-harness.sh`` first checks for a Node ≥ 18. On the CI runner
        # the check hits real node; on a developer laptop where PATH is
        # deliberately restricted (below), it wouldn't. A fake ``node``
        # reporting a recent major keeps the script off its apt/apk install
        # branch so the assertion is on what ``npm`` receives.
        fake_node = bin_dir / "node"
        fake_node.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            "  -e*process.exit*) exit 0 ;;\n"
            '  *) echo "v20.0.0" ;;\n'
            "esac\n"
        )
        fake_node.chmod(0o755)
        proc = subprocess.run(
            ["sh", str(INSTALL_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "TOLOKAFORGE_HARNESS_STATE_DIR": str(tmp_path / "state"),
                "TOLOKAFORGE_HARNESS_BIN_DIR": str(tmp_path / "target-bin"),
            },
        )
        recorded = record.read_text().splitlines() if record.exists() else []
        return proc, recorded

    @staticmethod
    def _recorded_version(tmp_path: Path) -> str:
        return (tmp_path / "state" / "installed-version.txt").read_text().strip()

    def test_every_shipped_harness_installs_via_its_declared_method(self, tmp_path):
        """Each shipped entry dispatches to whichever install_method it
        declares. The expected fake-recorder output shape depends on the
        method, so this test enumerates the per-method contracts once —
        each of the four dispatch tests below pins one branch in detail."""
        from tolokaforge_coding_harnesses import HARNESSES

        for name, spec in HARNESSES.items():
            proc, recorded = self._run_script(
                tmp_path / name, spec.install_method, spec.install_source, spec.version
            )
            assert proc.returncode == 0, f"{name}: {proc.stderr}"
            if spec.install_method == "npm":
                assert recorded == [f"install -g {spec.install_source}@{spec.version}"], name
            elif spec.install_method == "pip":
                assert recorded == [
                    f"install --no-cache-dir {spec.install_source}=={spec.version}"
                ], name
            elif spec.install_method == "curl-bash":
                assert recorded == [
                    f"-fsSL {spec.install_source} -o /tmp/harness-installer.sh",
                    f"installer {spec.version}",
                ], name
            elif spec.install_method == "binary":
                assert recorded == [f"-fsSL {spec.install_source} -o /tmp/harness-download"], name
            else:
                raise AssertionError(f"unknown install_method for {name}: {spec.install_method!r}")
            assert self._recorded_version(tmp_path / name) == spec.version, name

    def test_pip_install_dispatch_calls_pip(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "pip", "some-harness-cli", "1.2.3")
        assert proc.returncode == 0, proc.stderr
        assert recorded == ["install --no-cache-dir some-harness-cli==1.2.3"]
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_curl_bash_dispatch_runs_the_downloaded_installer(self, tmp_path):
        """The installer is downloaded and then run: POSIX sh has no
        ``pipefail``, so piping ``curl`` into ``sh`` would leave a failed
        download green with nothing installed."""
        proc, recorded = self._run_script(
            tmp_path, "curl-bash", "https://harness.invalid/install.sh", "1.2.3"
        )
        assert proc.returncode == 0, proc.stderr
        assert recorded == [
            "-fsSL https://harness.invalid/install.sh -o /tmp/harness-installer.sh",
            "installer 1.2.3",
        ]
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_binary_dispatch_installs_the_downloaded_executable(self, tmp_path):
        proc, recorded = self._run_script(
            tmp_path, "binary", "https://harness.invalid/dl/grok", "1.2.3"
        )
        assert proc.returncode == 0, proc.stderr
        assert recorded == ["-fsSL https://harness.invalid/dl/grok -o /tmp/harness-download"]
        installed = tmp_path / "target-bin" / "grok"
        assert os.access(installed, os.X_OK)
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_binary_dispatch_unpacks_a_tarball(self, tmp_path):
        """A `.tar.gz` source carries its executables at the archive root."""
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "grok").write_text("#!/bin/sh\necho grok\n")
        (payload / "grok").chmod(0o755)
        archive = tmp_path / "harness.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload / "grok", arcname="grok")

        proc, _ = self._run_script(
            tmp_path, "binary", "https://harness.invalid/dl/grok.tar.gz", "1.2.3", download=archive
        )
        assert proc.returncode == 0, proc.stderr
        assert os.access(tmp_path / "target-bin" / "grok", os.X_OK)

    def test_floating_version_aborts_for_a_downloaded_install(self, tmp_path):
        """Neither URL method can report what an installer chose, and an
        unrecorded agent version is not a benchmark result."""
        proc, recorded = self._run_script(
            tmp_path, "curl-bash", "https://harness.invalid/install.sh", "latest"
        )
        assert proc.returncode != 0
        assert "pin a version" in proc.stderr
        assert recorded == []

    def test_unknown_method_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "brew", "some-harness-cli", "1.2.3")
        assert proc.returncode != 0
        assert "unknown install method" in proc.stderr
        assert recorded == []

    def test_missing_version_aborts(self, tmp_path):
        """An unpinned install would make the agent version unrecorded."""
        proc, recorded = self._run_script(tmp_path, "npm", "@anthropic-ai/claude-code")
        assert proc.returncode != 0
        assert "pinned" in proc.stderr
        assert recorded == []

    def test_missing_source_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "npm")
        assert proc.returncode != 0
        assert "no install source" in proc.stderr
        assert recorded == []

    def test_missing_method_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path)
        assert proc.returncode != 0
        assert "no install method" in proc.stderr
        assert recorded == []
