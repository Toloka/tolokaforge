"""Static-grep enforcement: SecretManager is the sole credential entry point.

This test scans the engine, models and Langfuse packages for forbidden patterns and fails
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


_REPO_ROOT = Path(__file__).resolve().parents[3]
PKG_ROOT = _REPO_ROOT / "tolokaforge"
MODELS_PKG_ROOT = _REPO_ROOT / "tolokaforge_models" / "src" / "tolokaforge_models"
LANGFUSE_PKG_ROOT = _REPO_ROOT / "tolokaforge_langfuse" / "src" / "tolokaforge_langfuse"

#: Every package tree the guard scans. Extending the shipped surface area
#: with a plugin wheel means credential-read patterns there must be
#: caught by the same grep.
SCANNED_ROOTS: tuple[Path, ...] = (PKG_ROOT, MODELS_PKG_ROOT, LANGFUSE_PKG_ROOT)

# Files/dirs allowed to touch credentials directly.  These are the
# implementations of the SecretManager itself plus the runner-side bootstrap
# that reads the singleton-injection payload.
ALLOWLIST = {
    PKG_ROOT / "secrets",  # the implementation
    PKG_ROOT / "runner" / "__main__.py",  # singleton bootstrap from TOLOKAFORGE_SECRETS_JSON
    PKG_ROOT
    / "runner"
    / "llm_gateway_serve.py",  # sidecar bootstrap from TF_GATEWAY_UPSTREAM_TOKEN
    PKG_ROOT / "cli" / "main.py",  # init_default + export_to_environ at CLI startup
}


# Names whose access counts as a credential read. We use suffix-based matching
# so accidental aliases like ``MY_NEW_API_TOKEN`` still get caught.
_CREDENTIAL_PAT = re.compile(
    r"""(?xi)
    \b
    os\.(?:(?:environ\.get|getenv)\s*\(|environ\s*\[)\s*
    ["']                                            # opening quote
    (?P<name>[A-Z][A-Z0-9_]*?
        (?:_KEY|_API_KEYS|_API_BASE|_HEADERS
        |_TOKEN|_SECRET|_PASSWORD|_DSN|_CREDENTIAL[S]?|_PAT))
    ["']
    """,
)

# The same credential naming convention applies to constant arguments such as
# OTLP_HEADERS_SECRET, including subscript reads. Endpoint/config constants stay allowed.
_CREDENTIAL_CONSTANT_PAT = re.compile(
    r"\bos\.(?:(?:environ\.get|getenv)\s*\(|environ\s*\[)\s*"
    r"(?P<name>[A-Z][A-Z0-9_]*"
    r"(?:_KEY|_KEYS|_TOKEN|_SECRET|_PASSWORD|_DSN|_CREDENTIALS?|_HEADERS|_PAT))"
    r"(?=\s*[,\)\]])"
)

# Variable-arg form for credential-holding attributes:
# ``os.getenv(cert.env_key)`` / ``os.environ.get(binding.api_key_env)``.
# The string-literal grep above cannot catch these, but the pre-cutover
# scan for ``os.getenv(cert.env_key)`` in the certify fixtures is the
# exact reason this guard exists. Match variable-argument reads whose
# attribute name signals a credential holder — ``env_key`` /
# ``api_key_env`` / ``token_env`` / etc.  Config-shaped env reads
# (``os.getenv(port_var)``, ``os.environ.get(cache_dir_var)``) stay out
# of this pattern's scope.
_CREDENTIAL_VAR_ARG_PAT = re.compile(
    r"""(?xi)
    \b
    os\.(?:(?:environ\.get|getenv)\s*\(|environ\s*\[)\s*
    [A-Za-z_][A-Za-z_0-9.]*                         # dotted attribute chain
    \.(?:                                           # trailing attribute name
        env_key|api_key_env|api_keys_env
      |token_env|secret_env|password_env|credential_env
    )\b
    """,
)


_LOAD_DOTENV_PAT = re.compile(r"\bload_dotenv\s*\(")
_DOTENV_IMPORT_PAT = re.compile(r"^\s*from\s+dotenv\s+import\b", re.MULTILINE)


def _is_allowed(path: Path) -> bool:
    return any(path == p or p in path.parents for p in ALLOWLIST)


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not _is_allowed(p)]


def _iter_scanned_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        if not root.exists():
            continue
        files.extend(_python_files(root))
    return files


def _is_env_write(line: str, match: re.Match[str]) -> bool:
    return "[" in match.group(0) and bool(re.match(r"\s*\]\s*=(?!=)", line[match.end() :]))


def test_no_raw_credential_env_reads():
    offending: list[str] = []
    for path in _iter_scanned_python_files():
        text = path.read_text(encoding="utf-8")
        for line_num, line in enumerate(text.splitlines(), start=1):
            for match in _CREDENTIAL_PAT.finditer(line):
                if _is_env_write(line, match):
                    continue
                offending.append(
                    f"{path.relative_to(_REPO_ROOT)}:{line_num}: "
                    f"{match.group('name')} read via {match.group(0).strip()}"
                )
            for match in _CREDENTIAL_VAR_ARG_PAT.finditer(line):
                if _is_env_write(line, match):
                    continue
                offending.append(
                    f"{path.relative_to(_REPO_ROOT)}:{line_num}: "
                    f"variable-arg env read {match.group(0).strip()!r} — route "
                    f"through SecretManager.get_default().get_secret(...)"
                )
            for match in _CREDENTIAL_CONSTANT_PAT.finditer(line):
                if _is_env_write(line, match):
                    continue
                offending.append(
                    f"{path.relative_to(_REPO_ROOT)}:{line_num}: "
                    f"{match.group('name')} read via {match.group(0).strip()}"
                )
    assert not offending, (
        "Credential env reads outside tolokaforge.secrets — route through "
        "SecretManager.get_default().get_secret(...):\n  " + "\n  ".join(offending)
    )


def test_no_load_dotenv_calls():
    offending: list[str] = []
    for path in _iter_scanned_python_files():
        text = path.read_text(encoding="utf-8")
        for line_num, line in enumerate(text.splitlines(), start=1):
            if _LOAD_DOTENV_PAT.search(line):
                offending.append(f"{path.relative_to(_REPO_ROOT)}:{line_num}")
    assert not offending, (
        "load_dotenv() calls outside tolokaforge.secrets/cli bootstrap — "
        "use init_default() instead:\n  " + "\n  ".join(offending)
    )


def test_no_dotenv_imports():
    offending: list[str] = []
    for path in _iter_scanned_python_files():
        text = path.read_text(encoding="utf-8")
        if _DOTENV_IMPORT_PAT.search(text):
            offending.append(str(path.relative_to(_REPO_ROOT)))
    assert not offending, (
        "`from dotenv import ...` outside tolokaforge.secrets/cli — "
        "the dotenv package is a transitive dep of DotEnvProvider only:\n  "
        + "\n  ".join(offending)
    )


@pytest.mark.parametrize(
    "expression",
    [
        'os.environ.get("LANGFUSE_SECRET_KEY")',
        'os.getenv("LANGFUSE_PUBLIC_KEY")',
        'os.environ["LANGFUSE_SECRET_KEY"]',
        'os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")',
        "os.environ.get(OTLP_HEADERS_SECRET)",
        "os.getenv(LANGFUSE_SECRET_KEY_SECRET)",
        "os.environ[LANGFUSE_PUBLIC_KEY_SECRET]",
        "os.environ.get(binding.api_key_env)",
        "os.environ[cert.env_key]",
    ],
)
def test_guard_rejects_credential_reads_in_a_plugin(expression, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "credentials.py").write_text(f"value = {expression}\n")
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SCANNED_ROOTS", (plugin,))
    with pytest.raises(AssertionError, match="Credential env reads"):
        test_no_raw_credential_env_reads()


@pytest.mark.parametrize(
    "expression",
    [
        "os.environ.get(OTLP_TRACES_ENDPOINT_ENV)",
        'os.environ.get("LANGFUSE_BASE_URL")',
        "os.getenv(port_var)",
        "os.environ[binding.api_key_env] = new_key",
        "os.environ[OTLP_HEADERS_SECRET] = headers",
        'os.environ["LANGFUSE_SECRET_KEY"] = key',
    ],
)
def test_guard_allows_noncredential_configuration(expression, tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text(expression + "\n")
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SCANNED_ROOTS", (tmp_path,))
    test_no_raw_credential_env_reads()


def test_langfuse_sources_are_scanned():
    assert LANGFUSE_PKG_ROOT / "plugin.py" in _iter_scanned_python_files()
