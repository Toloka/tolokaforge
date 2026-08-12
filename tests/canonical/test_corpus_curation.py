"""``tolokaforge curate`` over recorded trials, on the shape a real run leaves on disk.

The two fixture runs under ``tests/data/curation_runs/`` are real recorded trials of the
notes duplicate-check criterion — one from a run that persisted its tool-call record and
one from a run predating that artifact — kept beside the corpus they would build rather
than under the gitignored ``results/`` tree the shipped corpus's own sources live in. A
lock against those sources would skip in CI, which is the same as not locking.

What the command has to get right on them:

1. **The write set is what a differential reads, plus the record where there was one** —
   six files curated from the record-carrying trial, five from the record-less one, and
   ``env.yaml`` copied from neither, though both sources carry it.
2. **A corpus is record-carrying exactly when its sources were**, and the manifest says
   so per bundle rather than leaving a reader to list the directory.
3. **Bundle directories are named ``<run-id>_trial<index>``, uniformly** — the name the
   committed corpus already carries for the same recorded trial.
4. **The manifest states the composition it wrote**: the criterion, both source runs,
   both task ids, and per bundle the judge's own recorded verdict and the models that
   produced the trial, each read off the bundle rather than off the invocation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from tolokaforge.core.grading.corpus_curation import (
    CORPUS_MANIFEST_FILENAME,
    CorpusManifest,
    CuratedBundle,
)
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_TEST_DATA = Path(__file__).resolve().parents[1] / "data"
_CURATION_RUNS = _TEST_DATA / "curation_runs"
_COMMITTED_CORPUS = _TEST_DATA / "migration_corpora" / "notes_duplicate_check"

_CRITERION = "checked_duplicates_first"
#: The run that persisted a tool-call record, and the one recorded before that artifact.
_RECORD_CARRYING_RUN = "native_shared_domain_policy_demo_20260803_063010"
_RECORD_LESS_RUN = "native_shared_domain_example_20260629_133126"

_DIFFERENTIAL_FILES = {
    "task.yaml",
    "trajectory.yaml",
    "grade.yaml",
    "tools_schemas.yaml",
    "metrics.yaml",
}


def _curate(*args: str) -> Result:
    return CliRunner().invoke(cli, ["curate", *args], env={"COLUMNS": "200"})


@pytest.fixture(scope="module")
def curated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The corpus one invocation builds from both fixture runs."""
    corpus = tmp_path_factory.mktemp("curated") / "notes_duplicate_check"
    result = _curate(
        "--source",
        str(_CURATION_RUNS / _RECORD_CARRYING_RUN),
        "--source",
        str(_CURATION_RUNS / _RECORD_LESS_RUN),
        "--into",
        str(corpus),
        "--criterion",
        _CRITERION,
    )
    assert result.exit_code == 0, result.output
    return corpus


@pytest.fixture(scope="module")
def manifest(curated: Path) -> CorpusManifest:
    return CorpusManifest.model_validate(
        yaml.safe_load((curated / CORPUS_MANIFEST_FILENAME).read_text())
    )


def _entry(manifest: CorpusManifest, run_id: str) -> CuratedBundle:
    (entry,) = [bundle for bundle in manifest.bundles if bundle.source_run == run_id]
    return entry


def test_a_record_carrying_source_curates_to_the_six_file_write_set(
    curated: Path, manifest: CorpusManifest
) -> None:
    """The record travels, and the trial's other artifacts do not.

    ``env.yaml`` sits beside the write set in the source bundle, so a copy of the
    directory would carry it and the write set does not.
    """
    entry = _entry(manifest, _RECORD_CARRYING_RUN)
    at_source = list((_CURATION_RUNS / _RECORD_CARRYING_RUN).rglob("env.yaml"))

    written = {path.name for path in (curated / entry.directory).iterdir()}

    assert written == _DIFFERENTIAL_FILES | {"tool_log.yaml"}
    assert at_source, "the source carries no env.yaml, so copying nothing extra proves nothing"
    assert entry.record_carried is True


def test_a_record_less_source_curates_to_the_five_file_write_set(
    curated: Path, manifest: CorpusManifest
) -> None:
    """A corpus is record-carrying exactly where its sources were, per bundle."""
    entry = _entry(manifest, _RECORD_LESS_RUN)

    written = {path.name for path in (curated / entry.directory).iterdir()}

    assert written == _DIFFERENTIAL_FILES
    assert entry.record_carried is False


def test_every_bundle_directory_is_named_run_id_then_trial_index(
    curated: Path, manifest: CorpusManifest
) -> None:
    """The naming rule, and the name the committed corpus already carries for one of them."""
    assert sorted(entry.directory for entry in manifest.bundles) == [
        f"{_RECORD_LESS_RUN}_trial0",
        f"{_RECORD_CARRYING_RUN}_trial0",
    ]
    assert sorted(path.name for path in curated.iterdir() if path.is_dir()) == sorted(
        entry.directory for entry in manifest.bundles
    )
    assert (_COMMITTED_CORPUS / "met" / f"{_RECORD_CARRYING_RUN}_trial0").is_dir()


def test_the_manifest_states_the_composition_it_wrote(manifest: CorpusManifest) -> None:
    """Every field is read off the bundles, not off the invocation."""
    assert manifest.criterion == _CRITERION
    assert manifest.curated_from == sorted([_RECORD_CARRYING_RUN, _RECORD_LESS_RUN])
    assert manifest.task_ids == [
        "notes_add_note_duplicate_check_gated",
        "notes_add_note_duplicate_check_policy",
    ]
    assert manifest.excluded == []

    carrying, record_less = (
        _entry(manifest, _RECORD_CARRYING_RUN),
        _entry(manifest, _RECORD_LESS_RUN),
    )
    assert (carrying.task_id, carrying.met, carrying.agent_model, carrying.judge_model) == (
        "notes_add_note_duplicate_check_policy",
        True,
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    )
    assert (
        record_less.task_id,
        record_less.met,
        record_less.agent_model,
        record_less.judge_model,
    ) == (
        "notes_add_note_duplicate_check_gated",
        False,
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4.1-mini",
    )
