"""Dev MCP server — tools for AI agents to interact with the tolokaforge repository."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from dev_mcp.subprocess_utils import run_command

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path:
    """Walk up from this file to find the repository root (contains pyproject.toml + tolokaforge/)."""
    candidate = Path(__file__).resolve()
    for parent in [candidate] + list(candidate.parents):
        if (parent / "pyproject.toml").exists() and (parent / "tolokaforge").is_dir():
            return parent
    # Fallback: assume CWD
    return Path.cwd()


REPO_ROOT = _find_repo_root()

# Default lint/format target directories (matches Makefile LINT_DIRS)
DEFAULT_LINT_DIRS = "tolokaforge tests scripts tools"

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _reload_dotenv() -> None:
    """Reload .env file on every tool invocation to pick up fresh values."""
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)


def _check_env_vars(*var_names: str) -> list[str]:
    """Return list of missing env var names."""
    return [v for v in var_names if not os.environ.get(v)]


def _has_any_llm_key() -> bool:
    """Check if at least one LLM API key is available."""
    keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"]
    return any(os.environ.get(k) for k in keys)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "tolokaforge-dev",
    instructions=(
        "Dev tools for the tolokaforge repository. "
        "Run tests, lint/format code, execute Python, validate tasks, and more. "
        "All commands run from the repository root."
    ),
)


# ---------------------------------------------------------------------------
# Tool: run_python
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_python(
    code: str = "",
    script_path: str = "",
    args: str = "",
) -> str:
    """Execute Python code or a script via `uv run python`.

    Provide either `code` (inline Python) or `script_path` (path to .py file).
    Optional `args` are space-separated arguments passed to the script.
    """
    _reload_dotenv()

    if not code and not script_path:
        return "Error: provide either `code` (inline Python) or `script_path` (path to .py file)"

    if code and script_path:
        return "Error: provide either `code` or `script_path`, not both"

    cmd: list[str] = ["uv", "run", "python"]

    if code:
        # Write code to a temp file and execute it
        tmp = Path(tempfile.mktemp(suffix=".py", prefix="dev_mcp_"))
        tmp.write_text(code)
        cmd.append(str(tmp))
    else:
        cmd.append(script_path)

    if args:
        cmd.extend(args.split())

    return await run_command(cmd, cwd=REPO_ROOT, timeout=120, tool_name="run_python")


# ---------------------------------------------------------------------------
# Tool: run_tests
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_tests(
    marker: str = "",
    path: str = "",
    keyword: str = "",
    extra_args: str = "",
) -> str:
    """Run pytest tests with optional filtering.

    Args:
        marker: Test marker to select (unit, canonical, integration).
                Leave empty to run all tests.
        path: Specific test file or directory (e.g. "tests/unit/test_diff.py").
              Leave empty for default test paths.
        keyword: Pytest -k keyword expression to filter tests.
        extra_args: Additional pytest arguments (space-separated).
    """
    _reload_dotenv()

    # Pre-flight check for integration tests
    if marker == "integration" and not _has_any_llm_key():
        return (
            "Error: integration tests require at least one LLM API key.\n"
            "Missing all of: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY.\n"
            "Add at least one to your .env file."
        )

    cmd: list[str] = ["uv", "run", "pytest"]

    if path:
        cmd.append(path)
    else:
        cmd.append("tests/")

    cmd.extend(["-v"])

    if marker:
        cmd.extend(["-m", marker])

    if keyword:
        cmd.extend(["-k", keyword])

    if extra_args:
        cmd.extend(extra_args.split())

    return await run_command(cmd, cwd=REPO_ROOT, timeout=300, tool_name="run_tests")


# ---------------------------------------------------------------------------
# Tool: update_canonical_snapshots
# ---------------------------------------------------------------------------


@mcp.tool()
async def update_canonical_snapshots(
    test_path: str = "",
) -> str:
    """Regenerate canonical test snapshots by running tests with --update-canon.

    Args:
        test_path: Specific canonical test file or directory.
                   Defaults to "tests/canonical/".
    """
    _reload_dotenv()

    target = test_path or "tests/canonical/"
    cmd: list[str] = ["uv", "run", "pytest", target, "-v", "--update-canon"]

    return await run_command(
        cmd, cwd=REPO_ROOT, timeout=300, tool_name="update_canonical_snapshots"
    )


# ---------------------------------------------------------------------------
# Tool: lint_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def lint_check(
    paths: str = "",
) -> str:
    """Check for linting issues without fixing (ruff check).

    Args:
        paths: Space-separated paths to check.
               Defaults to "tolokaforge tests scripts tools".
    """
    _reload_dotenv()

    targets = paths or DEFAULT_LINT_DIRS
    cmd: list[str] = ["uv", "run", "ruff", "check", *targets.split()]

    return await run_command(cmd, cwd=REPO_ROOT, timeout=60, tool_name="lint_check")


# ---------------------------------------------------------------------------
# Tool: lint_fix
# ---------------------------------------------------------------------------


@mcp.tool()
async def lint_fix(
    paths: str = "",
) -> str:
    """Auto-fix linting issues (ruff check --fix).

    Args:
        paths: Space-separated paths to fix.
               Defaults to "tolokaforge tests scripts tools".
    """
    _reload_dotenv()

    targets = paths or DEFAULT_LINT_DIRS
    cmd: list[str] = ["uv", "run", "ruff", "check", "--fix", *targets.split()]

    return await run_command(cmd, cwd=REPO_ROOT, timeout=60, tool_name="lint_fix")


# ---------------------------------------------------------------------------
# Tool: format_code
# ---------------------------------------------------------------------------


@mcp.tool()
async def format_code(
    paths: str = "",
) -> str:
    """Format code with ruff format.

    Args:
        paths: Space-separated paths to format.
               Defaults to "tolokaforge tests scripts tools".
    """
    _reload_dotenv()

    targets = paths or DEFAULT_LINT_DIRS
    target_list = targets.split()

    result = await run_command(
        ["uv", "run", "ruff", "format", *target_list],
        cwd=REPO_ROOT,
        timeout=60,
        tool_name="format_code_ruff",
    )

    return result


# ---------------------------------------------------------------------------
# Tool: format_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def format_check(
    paths: str = "",
) -> str:
    """Check code formatting without making changes (ruff format --check).

    Args:
        paths: Space-separated paths to check.
               Defaults to "tolokaforge tests scripts tools".
    """
    _reload_dotenv()

    targets = paths or DEFAULT_LINT_DIRS
    target_list = targets.split()

    result = await run_command(
        ["uv", "run", "ruff", "format", "--check", *target_list],
        cwd=REPO_ROOT,
        timeout=60,
        tool_name="format_check_ruff",
    )

    return result


# ---------------------------------------------------------------------------
# Tool: validate_tasks
# ---------------------------------------------------------------------------


@mcp.tool()
async def validate_tasks(
    glob_pattern: str = "",
) -> str:
    """Validate task YAML definitions using tolokaforge validate.

    Args:
        glob_pattern: Glob pattern for task files.
                      Defaults to "tasks/**/task.yaml".
    """
    _reload_dotenv()

    pattern = glob_pattern or "tasks/**/task.yaml"
    cmd: list[str] = ["uv", "run", "tolokaforge", "validate", "--tasks", pattern]

    return await run_command(cmd, cwd=REPO_ROOT, timeout=60, tool_name="validate_tasks")


# ---------------------------------------------------------------------------
# Tool: uv_sync
# ---------------------------------------------------------------------------


@mcp.tool()
async def uv_sync() -> str:
    """Install/sync all project dependencies using uv sync."""
    _reload_dotenv()

    cmd: list[str] = ["uv", "sync"]
    return await run_command(cmd, cwd=REPO_ROOT, timeout=120, tool_name="uv_sync")


# ---------------------------------------------------------------------------
# Tool: make_clean
# ---------------------------------------------------------------------------


@mcp.tool()
async def make_clean() -> str:
    """Clean build artifacts: __pycache__, *.pyc, *.egg-info, build/, dist/, .coverage, etc."""
    _reload_dotenv()

    cmd: list[str] = ["make", "clean"]
    return await run_command(cmd, cwd=REPO_ROOT, timeout=60, tool_name="make_clean")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the dev MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
