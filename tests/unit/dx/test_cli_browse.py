"""``tolokaforge browse`` — opens a run directory in the OS default handler.

Closes the design gap the end banner previously hinted at: the ``→ Browse:
tolokaforge browse <run-id>`` line now names a real, installed command.

Every test drives Click's :class:`CliRunner` with ``mix_stderr=False`` so
the stdout / stderr contract stays inspectable. ``webbrowser.open`` is
patched to capture the URL without actually launching a browser.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


class TestBrowseCommand:
    def test_absolute_path_opens_that_path(self, runner: CliRunner, tmp_path: Path) -> None:
        run_dir = tmp_path / "run_20260722_010203"
        run_dir.mkdir()

        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(cli, ["browse", str(run_dir)])

        assert result.exit_code == 0, result.stderr
        mock_open.assert_called_once()
        url = mock_open.call_args.args[0]
        assert url.startswith("file://")
        assert str(run_dir) in url
        assert url.endswith("/")

    def test_bare_run_id_resolves_against_default_results(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set up ./results/<run-id> under tmp_path and cd into it so the
        # default ``./results`` resolution kicks in.
        results_dir = tmp_path / "results" / "coding_20260722_010203"
        results_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(cli, ["browse", "coding_20260722_010203"])

        assert result.exit_code == 0, result.stderr
        url = mock_open.call_args.args[0]
        assert "coding_20260722_010203" in url
        assert url.endswith("/")

    def test_results_root_flag_overrides_default(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_root = tmp_path / "elsewhere"
        run_dir = custom_root / "run_x"
        run_dir.mkdir(parents=True)

        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(cli, ["browse", "run_x", "--results-root", str(custom_root)])

        assert result.exit_code == 0, result.stderr
        url = mock_open.call_args.args[0]
        assert str(run_dir) in url

    def test_results_root_env_var_overrides_default(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_root = tmp_path / "env-driven"
        run_dir = custom_root / "run_y"
        run_dir.mkdir(parents=True)
        monkeypatch.setenv("TOLOKAFORGE_RESULTS_ROOT", str(custom_root))

        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(cli, ["browse", "run_y"])

        assert result.exit_code == 0, result.stderr
        url = mock_open.call_args.args[0]
        assert str(run_dir) in url

    def test_missing_run_id_exits_nonzero_with_helpful_message(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no ./results directory

        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(cli, ["browse", "no-such-run-id"])

        assert result.exit_code != 0
        assert "No run directory found" in result.stderr
        assert "no-such-run-id" in result.stderr
        mock_open.assert_not_called()

    def test_stdout_stays_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        """browse is human-oriented; stdout must stay empty so it doesn't
        muddy the artifact-path shell-composition contract that `run` and
        `prepare` use."""
        run_dir = tmp_path / "run_z"
        run_dir.mkdir()

        with patch("webbrowser.open"):
            result = runner.invoke(cli, ["browse", str(run_dir)])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""

    def test_command_appears_in_root_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0, result.stderr
        # Grouped under Runs since it's a run-adjacent verb.
        assert "browse" in result.output


class TestBrowseTabCompletion:
    def test_completion_lists_matching_run_ids_under_default_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from tolokaforge.dx.cli.main import _complete_run_ids

        results = tmp_path / "results"
        for run_id in (
            "coding_20260722_010203",
            "coding_20260722_150000",
            "tool_use_run",
        ):
            (results / run_id).mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        ctx = MagicMock()
        ctx.params = {"results_root": None}
        assert _complete_run_ids(ctx, None, "coding") == [
            "coding_20260722_010203",
            "coding_20260722_150000",
        ]
        assert _complete_run_ids(ctx, None, "") == [
            "coding_20260722_010203",
            "coding_20260722_150000",
            "tool_use_run",
        ]

    def test_completion_honours_results_root_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from tolokaforge.dx.cli.main import _complete_run_ids

        other = tmp_path / "elsewhere"
        (other / "run_x").mkdir(parents=True)
        (other / "run_y").mkdir(parents=True)

        ctx = MagicMock()
        ctx.params = {"results_root": str(other)}
        assert _complete_run_ids(ctx, None, "run") == ["run_x", "run_y"]

    def test_completion_returns_empty_on_missing_results_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from tolokaforge.dx.cli.main import _complete_run_ids

        monkeypatch.chdir(tmp_path)  # no ./results directory

        ctx = MagicMock()
        ctx.params = {"results_root": None}
        # Must not raise — tab-completion inside a shell should degrade
        # silently if the results tree isn't there yet.
        assert _complete_run_ids(ctx, None, "anything") == []
