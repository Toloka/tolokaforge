"""Static-grep enforcement: SecretManager is the sole credential entry point.

This test scans the ``tolokaforge/`` package for forbidden patterns and fails
if any new offending lines are introduced. Adding a new violation will block
CI, forcing the contributor to either route through ``SecretManager`` or
add a new ``SecretProvider`` subclass.

The motivation is in AGENTS.md "Secrets — single abstraction": every
credential read in the codebase must go through ``tolokaforge.secrets``,
never via ``os.environ.get`` / ``os.getenv`` / ``load_dotenv`` / direct
``.env`` file reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


PKG_ROOT = Path(__file__).resolve().parents[3] / "tolokaforge"

# Files/dirs allowed to touch credentials directly.  These are the
# implementations of the SecretManager itself plus the runner-side bootstrap
# that reads the singleton-injection payload.
ALLOWLIST = {
    PKG_ROOT / "secrets",  # the implementation
    PKG_ROOT / "runner" / "__main__.py",  # singleton bootstrap from TOLOKAFORGE_SECRETS_JSON
    PKG_ROOT / "cli" / "main.py",  # init_default + export_to_environ at CLI startup
}


# Names whose access counts as a credential read. We use suffix-based matching
# so accidental aliases like ``MY_NEW_API_TOKEN`` still get caught.
_CREDENTIAL_PAT = re.compile(
    r"""(?xi)
    \b
    os\.(?:environ\.get|getenv)
    \s*\(\s*
    ["']                                            # opening quote
    (?P<name>[A-Z][A-Z0-9_]*?
        (?:_API_KEY|_API_KEYS|_API_BASE
        |_TOKEN|_SECRET|_PASSWORD|_DSN|_CREDENTIAL[S]?|_PAT))
    ["']
    """,
)


_LOAD_DOTENV_PAT = re.compile(r"\bload_dotenv\s*\(")
_DOTENV_IMPORT_PAT = re.compile(r"^\s*from\s+dotenv\s+import\b", re.MULTILINE)


def _is_allowed(path: Path) -> bool:
    return any(path == p or p in path.parents for p in ALLOWLIST)


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not _is_allowed(p)]


def test_no_raw_credential_env_reads():
    offending: list[str] = []
    for path in _python_files(PKG_ROOT):
        text = path.read_text(encoding="utf-8")
        for line_num, line in enumerate(text.splitlines(), start=1):
            for match in _CREDENTIAL_PAT.finditer(line):
                offending.append(
                    f"{path.relative_to(PKG_ROOT.parent)}:{line_num}: "
                    f"{match.group('name')} read via {match.group(0).strip()}"
                )
    assert not offending, (
        "Credential env reads outside tolokaforge.secrets — route through "
        "SecretManager.get_default().get_secret(...):\n  " + "\n  ".join(offending)
    )


def test_no_load_dotenv_calls():
    offending: list[str] = []
    for path in _python_files(PKG_ROOT):
        text = path.read_text(encoding="utf-8")
        for line_num, line in enumerate(text.splitlines(), start=1):
            if _LOAD_DOTENV_PAT.search(line):
                offending.append(f"{path.relative_to(PKG_ROOT.parent)}:{line_num}")
    assert not offending, (
        "load_dotenv() calls outside tolokaforge.secrets/cli bootstrap — "
        "use init_default() instead:\n  " + "\n  ".join(offending)
    )


def test_no_dotenv_imports():
    offending: list[str] = []
    for path in _python_files(PKG_ROOT):
        text = path.read_text(encoding="utf-8")
        if _DOTENV_IMPORT_PAT.search(text):
            offending.append(str(path.relative_to(PKG_ROOT.parent)))
    assert not offending, (
        "`from dotenv import ...` outside tolokaforge.secrets/cli — "
        "the dotenv package is a transitive dep of DotEnvProvider only:\n  "
        + "\n  ".join(offending)
    )
