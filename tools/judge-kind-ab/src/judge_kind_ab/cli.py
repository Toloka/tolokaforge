"""CLI for judge-kind-ab — live kappa-parity + cost A/B across JudgeKinds.

Phase C4 of ``docs/JUDGE_KINDS.md`` § Live A/B. Drives the kappa-parity harness
(``measure_cross_kind_agreement``, ``measure_self_consistency``) against a live
judge model over real completed trials, instead of the canonical lane's
committed cassettes, and renders the resulting per-criterion kappa + cost
tables.

Why a ``tools/`` workspace member (not a ``tolokaforge`` CLI subcommand):
mirrors ``rubric-calibrator`` — self-contained Python with its own deps that
runs real inference offline from the runner stack. ``scripts/analysis/
run_judge_kind_ab.sh`` wraps it with the repo's ``.env`` loader.

Secrets: this is the process entrypoint that spends money, so — like
``rubric_calibrator.cli`` — it initialises the ``SecretManager`` singleton and
mirrors provider keys into ``os.environ`` (litellm reads them there). All
actual key reads still go through ``SecretManager`` inside ``LLMClient``; this
module never reads a credential directly.

A below-threshold kappa is an annotation on the rendered table, not a hard
failure — this command always exits 0, since it is a one-off evidence-
gathering tool, not a CI gate any task pack depends on.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from tolokaforge.core.grading.default_judge_model_provider import LiteLLMJudgeModelProvider
from tolokaforge.core.grading.judge import model_config_from_ref
from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider

from .bundle_corpus import load_corpus_from_run
from .report import write_report
from .runner import run_live_ab

app = typer.Typer(help="Live kappa-parity + cost A/B across JudgeKinds.")
console = Console()

#: Cheap, tool-calling-capable default judge model (the calibrator's choice).
DEFAULT_MODEL_REF = "openrouter/openai/gpt-4.1-mini"
DEFAULT_KINDS = "single_shot_rubric,chunked_rubric,agentic_rubric"

#: Provider keys litellm looks up via os.environ; mirrored once at startup.
_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


def _init_secrets() -> None:
    """Initialise the SecretManager singleton and mirror provider keys to environ.

    Mirrors ``rubric_calibrator.cli`` startup: all reads still go through
    ``SecretManager``; this only makes litellm's ``os.environ`` lookups resolve.
    """
    from tolokaforge.secrets import init_default

    secrets = init_default()
    secrets.export_to_environ(list(_PROVIDER_KEYS))


@app.callback()
def _main() -> None:
    """judge-kind-ab: live JudgeKind kappa-parity + cost A/B."""


@app.command()
def run(
    bundles_dir: Path = typer.Argument(
        ...,
        help="Directory of <family>/<bundle>/ completed-trial grade bundles.",
    ),
    model_ref: str = typer.Option(
        DEFAULT_MODEL_REF,
        "--model-ref",
        "-m",
        help="Judge model ref '<provider>/<model>'. Default is a cheap small model.",
    ),
    kinds: str = typer.Option(
        DEFAULT_KINDS,
        "--kinds",
        help="Comma-separated JudgeKind entry-point names to A/B.",
    ),
    replays: int = typer.Option(
        5,
        "--replays",
        help="Self-consistency replay count (>= 2).",
    ),
    out_dir: Path = typer.Option(
        ...,
        "--out-dir",
        help="Directory to write report.md / report.json into.",
    ),
) -> None:
    """Measure cross-kind kappa agreement, self-consistency, and cost."""
    _init_secrets()

    kind_names = [name.strip() for name in kinds.split(",") if name.strip()]
    corpus = load_corpus_from_run(bundles_dir)
    console.print(
        f"[bold]Live A/B[/bold]: {len(corpus)} bundle(s), kinds={kind_names}, "
        f"judge=[cyan]{model_ref}[/cyan], replays={replays}"
    )

    judge_model_config = model_config_from_ref(model_ref)

    def _provider_factory_for(_name: str):
        def _factory(_replay_index: int) -> JudgeModelProvider:
            return LiteLLMJudgeModelProvider()

        return _factory

    result = run_live_ab(
        corpus,
        kind_names=kind_names,
        replays=replays,
        judge_model_config=judge_model_config,
        provider_factory_for=_provider_factory_for,
    )

    write_report(result, out_dir)
    console.print(f"[green]Wrote[/green] {out_dir / 'report.md'} and {out_dir / 'report.json'}")


def main() -> None:
    """Entrypoint."""
    app()
