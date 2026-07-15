"""Byte-level canonical SVG goldens for the ``--dry-run`` panel.

Each golden pins the exact ``Console.export_svg`` bytes Rich produces
when :func:`render_dry_run_preamble` and :func:`render_dry_run_sample`
are called against a recording console at fixed 80- and 120-column
widths. The two widths keep the AC's "renders correctly on 80- and
120-column terminals" assertion honest under Rich version drift.

The fixture is a hand-built :class:`DryRunSample` — the goldens pin the
rendering layer, not the materialization pipeline. Populate every field
with a deterministic value (literal user message, two builtin-shaped
tool specs, resolved agent line, no judge, shared runtime) so the SVG
exercises every body element the panel can display.

**Golden regeneration.**

    uv run pytest tests/canonical/test_dry_run_goldens.py --update-canon

Determinism knobs mirror ``test_run_display_goldens.py`` and
``test_run_banner_goldens.py``:

* ``unique_id="tolokaforge-dry-run"`` fixes the CSS class prefix.
* ``theme=DEFAULT_TERMINAL_THEME`` pins the palette embedded in the
  ``<style>`` block.
* ``force_terminal=True`` + ``color_system="truecolor"`` bypass ambient
  capability probes.
* ``theme=THEME`` on the recorder resolves ``[muted]…[/muted]`` markup
  the same way the shared ``console`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from rich.terminal_theme import DEFAULT_TERMINAL_THEME

from tolokaforge.core.dry_run import DryRunSample
from tolokaforge.dx._display import THEME
from tolokaforge.dx.dry_run_render import (
    render_dry_run_preamble,
    render_dry_run_sample,
)

pytestmark = pytest.mark.canonical

GOLDEN_DIR = Path(__file__).parent / "golden" / "dry_run"

SVG_UNIQUE_ID = "tolokaforge-dry-run"

_WIDTHS: tuple[int, ...] = (80, 120)


_FIXTURE_SAMPLE = DryRunSample(
    task_id="tool_use_public_example_01",
    trial_index=0,
    system_prompt=(
        "You are a helpful assistant.\nUse the available tools when they help the user."
    ),
    user_prompt_text="Please look up order T-100 in the system.",
    user_prompt_is_literal=True,
    tool_spec=[
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 file from the sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List entries in a sandbox directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
    ],
    agent_model_line=("openrouter/anthropic/claude-sonnet-4.6 · preset: anthropic_claude_4_7"),
    judge_model_line="(none)",
    runtime_line="shared",
)


def _make_recorder(width: int) -> Console:
    return Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        theme=THEME,
    )


def _render(width: int) -> str:
    recorder = _make_recorder(width)
    render_dry_run_preamble(n_rendered=1, n_available=3, console=recorder)
    render_dry_run_sample(sample=_FIXTURE_SAMPLE, console=recorder)
    return recorder.export_svg(
        title="tolokaforge run --dry-run",
        theme=DEFAULT_TERMINAL_THEME,
        unique_id=SVG_UNIQUE_ID,
    )


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"width{w}" for w in _WIDTHS])
def test_dry_run_panel_matches_golden(request: pytest.FixtureRequest, width: int) -> None:
    """The rendered SVG matches ``panel_{width}.svg`` byte-for-byte."""

    actual = _render(width)
    golden_path = GOLDEN_DIR / f"panel_{width}.svg"

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_dry_run_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for panel_{width}.svg — re-run with "
            "`--update-canon` if the change is intentional, then review the "
            "diff before committing."
        )


def test_dry_run_render_module_exports_public_surface() -> None:
    """The three render helpers stay importable from :mod:`_dry_run_render`.

    ``render_dry_run``, ``render_dry_run_sample``, and
    ``render_dry_run_preamble`` are the names ``tolokaforge.dx.cli.main``
    wires into the ``run --dry-run`` branch — a silent rename would break
    the CLI. Pin the surface here so the failure surfaces at canonical
    time rather than at the runtime call site.
    """
    from tolokaforge.dx.dry_run_render import (
        render_dry_run,
        render_dry_run_preamble,
        render_dry_run_sample,
    )

    assert callable(render_dry_run)
    assert callable(render_dry_run_preamble)
    assert callable(render_dry_run_sample)
