"""Unit tests for the all-in-one image entrypoint's TypeSense env wiring.

``setup_typesense_env`` in
``tolokaforge/docker/dockerfiles/standalone/entrypoint.sh`` is load-bearing: the
bundled TypeSense program's command uses ``%(ENV_TYPESENSE_API_KEY)s``, which
supervisord refuses to expand if the key is unset — aborting *every* supervised
program, not just TypeSense. So a regression that stops this function defaulting
the key would brick the whole image. These tests exercise the function directly
(the entrypoint guards its dispatch behind ``BASH_SOURCE == $0`` so it can be
sourced) across the enabled / disabled / pre-set paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ENTRYPOINT = (
    Path(__file__).resolve().parents[2] / "tolokaforge/docker/dockerfiles/standalone/entrypoint.sh"
)


def _run_setup(env: dict[str, str]) -> dict[str, str]:
    """Source the entrypoint, run ``setup_typesense_env``, return the TS_* env it set."""
    script = (
        f"source '{_ENTRYPOINT}'\n"
        "setup_typesense_env\n"
        'printf "HOST=%s\\n" "${TYPESENSE_HOST:-<unset>}"\n'
        'printf "PORT=%s\\n" "${TYPESENSE_PORT:-<unset>}"\n'
        'printf "KEY=%s\\n" "${TYPESENSE_API_KEY:-<unset>}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
    )
    assert proc.returncode == 0, f"setup_typesense_env failed: {proc.stderr}"
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_enabled_defaults_host_port_and_generates_key() -> None:
    """With the flavor on and no key, it defaults host/port and mints a non-empty key."""
    out = _run_setup({"TYPESENSE_ENABLED": "true"})
    assert out["HOST"] == "localhost"
    assert out["PORT"] == "8108"
    assert out["KEY"] != "<unset>" and len(out["KEY"]) >= 32, (
        "a non-empty TYPESENSE_API_KEY must be defaulted so supervisord's "
        "%(ENV_TYPESENSE_API_KEY)s expansion resolves"
    )


def test_enabled_preserves_operator_supplied_key() -> None:
    """A caller-supplied key is kept verbatim (host indexer + runner must match)."""
    out = _run_setup({"TYPESENSE_ENABLED": "true", "TYPESENSE_API_KEY": "operator-key-123"})
    assert out["KEY"] == "operator-key-123"
    assert out["HOST"] == "localhost"
    assert out["PORT"] == "8108"


def test_enabled_preserves_explicit_host_and_port() -> None:
    """Explicit host/port are not overridden."""
    out = _run_setup(
        {"TYPESENSE_ENABLED": "true", "TYPESENSE_HOST": "ts.internal", "TYPESENSE_PORT": "9999"}
    )
    assert out["HOST"] == "ts.internal"
    assert out["PORT"] == "9999"


def test_disabled_is_a_noop() -> None:
    """With the flavor off (lean image), it touches no TYPESENSE_* env."""
    out = _run_setup({})  # TYPESENSE_ENABLED unset
    assert out == {"HOST": "<unset>", "PORT": "<unset>", "KEY": "<unset>"}
