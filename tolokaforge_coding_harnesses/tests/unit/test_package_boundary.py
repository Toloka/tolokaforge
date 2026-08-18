"""The package stands alone: no engine import, and its data ships with it.

Two invariants, and neither is expressible as a dependency declaration. Any
runtime — this repo's terminal-bench adapter, a second adapter, a runner-side
consumer — must be able to read the registry without pulling in an engine and
its version pin, and the shipped YAML must resolve as a package sibling however
the wheel was installed.
"""

import subprocess
import sys

import pytest

from tolokaforge_coding_harnesses import (
    HARNESSES,
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_SCRIPT,
    SHIPPED_REGISTRY_FILE,
    SHIPPED_REGISTRY_META_FILE,
)

pytestmark = pytest.mark.unit

_SHIPPED_HARNESS_NAMES = {
    "claude-code",
    "codex",
    "gemini-cli",
    "grok-build",
    "kimi-code",
    "opencode",
}


def test_importing_the_package_pulls_in_no_engine_module() -> None:
    """A fresh interpreter, so the engine cannot arrive via a neighbour's import.

    In-process this would pass by accident: pytest has already imported the
    engine for the suites that need it. The subprocess is the only place the
    claim is testable, and a stray ``import tolokaforge`` added later fails
    here rather than in whichever consumer first installs the package without
    the engine.
    """
    probe = (
        "import sys, tolokaforge_coding_harnesses, tolokaforge_coding_harnesses.fingerprint\n"
        "leaked = sorted(m for m in sys.modules if m == 'tolokaforge' "
        "or m.startswith('tolokaforge.'))\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", (
        f"importing tolokaforge_coding_harnesses pulled in engine module(s) "
        f"{result.stdout.strip()}; the package is consumed by runtimes that do not "
        "install the engine, so it must not import one."
    )


def test_the_shipped_data_resolves_as_a_package_sibling() -> None:
    """All four shipped siblings sit beside the module that reads them.

    The constants resolve relative to ``__file__``, so a wrong number of
    ``parent`` hops escapes the package and every read fails at import — including
    inside an installed wheel, where there is no repo layout to fall back on. The
    scripts are asserted alongside the YAML files because
    ``compose_synthesis._write_harness_build_context`` copies both into the trial
    image and a hatchling misconfiguration that dropped the non-Python siblings
    would ship a broken wheel.
    """
    assert SHIPPED_REGISTRY_FILE.is_file(), f"{SHIPPED_REGISTRY_FILE} is not a file"
    assert SHIPPED_REGISTRY_META_FILE.is_file(), f"{SHIPPED_REGISTRY_META_FILE} is not a file"
    assert INSTALL_SCRIPT.is_file(), f"{INSTALL_SCRIPT} is not a file"
    assert MIDDLEWARE_PROXY_SCRIPT.is_file(), f"{MIDDLEWARE_PROXY_SCRIPT} is not a file"


def test_the_shipped_registry_declares_exactly_the_six_documented_harnesses() -> None:
    """The load-time proof that the packaged data is the data that was read."""
    assert set(HARNESSES) == _SHIPPED_HARNESS_NAMES
