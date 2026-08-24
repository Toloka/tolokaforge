"""The terminal-bench adapter's ``PathResolver`` seam.

An embedder driving the adapter from Python supplies its runtime's answer, and
the *same* registry data lands somewhere else. The resolver's own clauses and the
shipped registry's vocabulary are covered by
``tolokaforge_coding_harnesses/tests/unit/test_path_resolvers.py``.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]

_FIXTURE_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"


@dataclass(frozen=True)
class _HomeAgentResolver:
    """A second runtime's answer: the CLI runs as a non-root user."""

    def resolve(self, path: str) -> str:
        return path.replace("${HOME}", "/home/agent")


class TestTheResolverSeamIsLoadBearing:
    """An embedder driving the adapter from Python supplies its runtime's
    answer, and the *same* registry data lands somewhere else."""

    @pytest.fixture
    def overlay(self, tmp_path) -> Path:
        path = tmp_path / "harness_presets.yaml"
        path.write_text(
            "harnesses:\n"
            "  my-cli:\n"
            "    install_source: '@acme/my-cli'\n"
            "    version: '1.0.0'\n"
            "    argv_prefix: [my-cli]\n"
            "    argv_suffix: ['--run']\n"
            "    config_files:\n"
            '      "${HOME}/.cli/config.toml": \'model = "{{ model }}"\'\n'
        )
        return path

    @staticmethod
    def _command(overlay: Path, tmp_path: Path, **extra) -> str:
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(_FIXTURE_DIR),
                "staging_root": str(tmp_path / "staging"),
                "harness_presets_file": str(overlay),
                "agent_harness": "my-cli",
                "agent_model": "m",
            },
            **extra,
        )
        return adapter.to_task_description("echo-hello").metadata["agent_harness_command"]

    def test_an_injected_resolver_decides_where_the_config_file_is_written(self, overlay, tmp_path):
        command = self._command(overlay, tmp_path, path_resolver=_HomeAgentResolver())
        assert "/home/agent/.cli/config.toml" in command
        assert "${HOME}" not in command

    def test_without_the_kwarg_the_shipped_resolver_answers(self, overlay, tmp_path):
        command = self._command(overlay, tmp_path)
        assert "/root/.cli/config.toml" in command
        assert "${HOME}" not in command
