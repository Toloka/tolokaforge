"""Guards the structural invariants a Codespace/dev-container boot depends on.

The dev-container boots by running three lifecycle hooks declared in
``.devcontainer/devcontainer.json``; those hooks in turn invoke the reused
``scripts/setup/*`` scripts. A hook script that is renamed, loses its execute
bit, or drops the ``scripts/setup`` invocation would break a fresh Codespace
boot silently — no other test exercises this wiring. This guard turns each such
regression into a CI failure. Runs under the ``canonical`` marker so it rides
the existing smoke job without dedicated workflow wiring.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVCONTAINER_DIR = _REPO_ROOT / ".devcontainer"
_DEVCONTAINER_JSON = _DEVCONTAINER_DIR / "devcontainer.json"
_README = _REPO_ROOT / "README.md"

_LIFECYCLE_HOOKS = ("initializeCommand", "postCreateCommand", "postAttachCommand")

# devcontainer.json is JSONC: only `//` line comments, no `//` inside string
# values. A minimal line-comment strip makes it parseable by json.loads.
_LINE_COMMENT_RE = re.compile(r"(^|\s)//[^\n]*")
_CODESPACES_HEADING_RE = re.compile(r"^#{1,6}\s.*codespace", re.IGNORECASE | re.MULTILINE)


def _load_devcontainer() -> dict:
    assert _DEVCONTAINER_JSON.exists(), (
        f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)} is missing — "
        "the dev-container definition must exist"
    )
    raw = _DEVCONTAINER_JSON.read_text()
    stripped = _LINE_COMMENT_RE.sub(lambda m: m.group(1), raw)
    config = json.loads(stripped)
    assert config, (
        f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)} parsed to an empty document — "
        "expected a populated dev-container config object"
    )
    return config


def test_devcontainer_json_parses() -> None:
    config = _load_devcontainer()
    assert isinstance(config, dict), (
        f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)} must be a JSON object, "
        f"got {type(config).__name__}"
    )


def test_lifecycle_hooks_resolve_to_executable_scripts() -> None:
    config = _load_devcontainer()
    violations: list[str] = []
    for hook in _LIFECYCLE_HOOKS:
        command = config.get(hook)
        if not command:
            violations.append(
                f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)}: lifecycle hook "
                f"`{hook}` is missing — a Codespace boot requires it"
            )
            continue
        script_rel = command.split()[0]
        script = _REPO_ROOT / script_rel
        if not script.exists():
            violations.append(
                f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)}: hook `{hook}` points at "
                f"`{script_rel}`, which does not exist under the repo root"
            )
        elif not os.access(script, os.X_OK):
            violations.append(
                f"{_DEVCONTAINER_JSON.relative_to(_REPO_ROOT)}: hook `{hook}` script "
                f"`{script_rel}` is not executable — run `chmod +x {script_rel}`"
            )
    assert not violations, "\n".join(violations)


def test_reused_setup_scripts_exist_and_are_wired() -> None:
    violations: list[str] = []
    for script_rel in ("scripts/setup/create_python_venv.sh", "scripts/setup/init_git_lfs.sh"):
        script = _REPO_ROOT / script_rel
        if not script.exists():
            violations.append(f"{script_rel} is missing — the dev-container hooks depend on it")
        elif not os.access(script, os.X_OK):
            violations.append(f"{script_rel} is not executable — run `chmod +x {script_rel}`")

    wiring = (
        (".devcontainer/post_attach_container.sh", "create_python_venv.sh"),
        (".devcontainer/post_setup_container.sh", "init_git_lfs.sh"),
    )
    for hook_rel, expected_call in wiring:
        hook = _REPO_ROOT / hook_rel
        if not hook.exists():
            violations.append(f"{hook_rel} is missing — the dev-container hook must exist")
            continue
        active = "\n".join(
            line for line in hook.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        if expected_call not in active:
            violations.append(
                f"{hook_rel} no longer invokes `{expected_call}` — the dev-container "
                "boot would skip that setup step silently"
            )
    assert not violations, "\n".join(violations)


def test_readme_documents_codespaces() -> None:
    assert _README.exists(), f"{_README.relative_to(_REPO_ROOT)} is missing"
    assert _CODESPACES_HEADING_RE.search(_README.read_text()), (
        f"{_README.relative_to(_REPO_ROOT)} has no Codespaces heading — expected a markdown "
        "heading matching `^#{1,6}\\s.*codespace` documenting the dev-container flow"
    )
