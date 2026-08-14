"""Unit tests for the terminal-bench adapter's ``PathResolver`` seam."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
from tolokaforge_adapter_terminal_bench.harness import (
    DEFAULT_PATH_RESOLVER,
    HARNESSES,
    LinuxRootResolver,
)
from tolokaforge_adapter_terminal_bench.harness.protocols import PATH_CONSTRUCT_PATTERN

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]

_FIXTURE_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

_DEFERRED_BY_DESIGN = frozenset({"CODEX_HOME"})
"""Variables the shipped registry leaves for the container's own shell.

``CODEX_HOME`` is the codex CLI's own override: only the container knows
whether an operator set it, so no resolver can answer it without overriding a
user's explicit choice. Every other construct in the shipped registry must be
in the resolver's vocabulary.
"""


class TestLinuxRootResolver:
    """The shipped default: a task container running its CLI as ``root``."""

    def test_a_path_without_a_construct_comes_back_unchanged(self):
        assert LinuxRootResolver().resolve("/root/.claude") == "/root/.claude"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("${HOME}/.claude", "/root/.claude"),
            ("${CONFIG_HOME}/opencode/opencode.json", "/root/.config/opencode/opencode.json"),
        ],
    )
    def test_a_known_variable_is_replaced_by_the_resolvers_value(self, path, expected):
        assert LinuxRootResolver().resolve(path) == expected

    def test_a_default_clause_on_a_known_variable_is_discarded(self):
        """The resolver is the authority on its own vocabulary, so a registry
        entry's fallback never gets to override it."""
        assert LinuxRootResolver().resolve("${HOME:-/elsewhere}/x") == "/root/x"

    @pytest.mark.parametrize(
        "path",
        [
            "${CODEX_HOME:-$HOME/.codex}/config.toml",
            "${MY_CLI_HOME}/config.toml",
        ],
    )
    def test_an_unknown_variable_is_deferred_to_the_containers_shell(self, path):
        """This runtime has a POSIX shell, so it defers rather than raising —
        which is what keeps an operator overlay naming its own variable working
        exactly as it does without a resolver."""
        assert LinuxRootResolver().resolve(path) == path


def _unresolved_variables(path: str) -> set[str]:
    """Variable names still carried by *path* after the shipped resolver ran."""
    resolved = DEFAULT_PATH_RESOLVER.resolve(path)
    return {construct.group(1) for construct in PATH_CONSTRUCT_PATTERN.finditer(resolved)}


class TestTheShippedRegistrysConstructsAreInTheVocabulary:
    """A mistyped construct loads fine — the resolver defers rather than
    raising — so these tests are where ``${CONFG_HOME}`` fails. Anyone
    expecting a load-time error for a bad variable name is looking in the wrong
    place."""

    def test_every_config_files_key_resolves_or_is_deferred_by_design(self):
        paths = [(name, path) for name, spec in HARNESSES.items() for path in spec.config_files]
        assert paths, "the shipped registry declares no config_files path to check"
        unaccounted = {}
        for name, path in paths:
            undeclared = _unresolved_variables(path) - _DEFERRED_BY_DESIGN
            if undeclared:
                unaccounted[f"{name}: {path}"] = sorted(undeclared)
        assert not unaccounted, (
            f"config_files path(s) {unaccounted} name a variable the shipped resolver does "
            "not know; add it to the resolver's vocabulary, or declare it in "
            "_DEFERRED_BY_DESIGN with the reason only the container can answer it."
        )

    def test_every_skills_dir_target_resolves_fully(self):
        """No deferral allow-list here: a Dockerfile ``COPY`` destination is
        consumed by Docker, which expands variables from the image's own
        ``ENV`` — not the container shell's answer, and not the resolver's."""
        unresolved = {}
        for name, spec in HARNESSES.items():
            if spec.skills_dir_target is None:
                continue
            undeclared = _unresolved_variables(spec.skills_dir_target)
            if undeclared:
                unresolved[f"{name}: {spec.skills_dir_target}"] = sorted(undeclared)
        assert not unresolved, (
            f"skills_dir_target(s) {unresolved} still carry a construct after the shipped "
            "resolver ran; a skills target cannot be deferred."
        )


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
