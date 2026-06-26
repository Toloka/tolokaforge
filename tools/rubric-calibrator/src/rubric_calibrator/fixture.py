"""Golden calibration fixture — schema + loader.

Stage 6 of ``docs/RUBRIC_GRADING_DESIGN.md``. A fixture is the human-authored ground
truth a rubric judge is calibrated against. It bundles everything
``run_rubric_judge`` needs to grade one episode plus the human's per-criterion
labels to compare against:

* ``rubric`` — the structured :class:`~tolokaforge.runner.models.Rubric` under test;
* ``transcript`` — the agent's messages (role/content/tool_calls), including the
  policy/system message context is carried separately via ``agent_system_prompt``;
* ``final_db_state`` — optional tables for the in-memory ``DBReader`` (reused from
  the Stage-4 live test pattern) so the judge can inspect final state with no
  runner stack;
* ``workspace`` — optional path (relative to the fixture file) the judge's
  ``read_file`` tool reads, for tasks that produce files;
* ``rag_url`` — optional, for RAG-backed tasks;
* ``expected`` — the human labels: one :class:`ExpectedVerdict` per criterion.

Crosses a YAML serialisation boundary, so the schema is Pydantic v2 with
``extra="forbid"`` (AGENTS type-system table). Loading fails loud on any unknown
key, missing criterion label, or label for a criterion not in the rubric.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator

from tolokaforge.runner.models import Rubric


class ExpectedVerdict(BaseModel):
    """Human ground-truth label for one criterion.

    Exactly one of ``met`` / ``score`` is required, matching the criterion kind:
    ``met`` (bool) for a binary criterion, ``score`` (0–1) for a graded one. The
    loader cross-checks the kind against the rubric. ``note`` is optional context
    for whoever authored the label (why this is the correct verdict).
    """

    criterion_id: str
    met: bool | None = None
    score: float | None = None
    note: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _exactly_one_verdict(self) -> ExpectedVerdict:
        if (self.met is None) == (self.score is None):
            raise ValueError(
                f"Expected verdict for '{self.criterion_id}' must set exactly one of "
                "'met' (binary) or 'score' (graded)."
            )
        if self.score is not None and not (0.0 <= self.score <= 1.0):
            raise ValueError(
                f"Expected score for '{self.criterion_id}' must be in [0, 1], got {self.score}."
            )
        return self


class GoldenFixture(BaseModel):
    """One human-labelled calibration episode for a rubric judge."""

    id: str
    description: str | None = None
    rubric: Rubric
    agent_system_prompt: str = ""
    transcript: list[dict[str, Any]]
    final_db_state: dict[str, Any] | None = None
    workspace: str | None = None
    rag_url: str | None = None
    expected: list[ExpectedVerdict]

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _labels_match_rubric(self) -> GoldenFixture:
        """Fail loud unless there is exactly one label per rubric criterion, with
        the verdict shape matching the criterion kind."""
        criterion_kinds = {c.id: c.kind for c in self.rubric.criteria}
        labelled = [e.criterion_id for e in self.expected]

        duplicates = {cid for cid in labelled if labelled.count(cid) > 1}
        if duplicates:
            raise ValueError(
                f"Fixture {self.id!r} has duplicate expected labels: {sorted(duplicates)}."
            )

        labelled_set = set(labelled)
        missing = set(criterion_kinds) - labelled_set
        extra = labelled_set - set(criterion_kinds)
        if missing:
            raise ValueError(
                f"Fixture {self.id!r} is missing expected labels for: {sorted(missing)}."
            )
        if extra:
            raise ValueError(
                f"Fixture {self.id!r} has expected labels for unknown criteria: {sorted(extra)}."
            )

        for verdict in self.expected:
            kind = criterion_kinds[verdict.criterion_id]
            if kind == "binary" and verdict.met is None:
                raise ValueError(
                    f"Criterion '{verdict.criterion_id}' is binary; its expected verdict must "
                    "set 'met', not 'score'."
                )
            if kind == "graded" and verdict.score is None:
                raise ValueError(
                    f"Criterion '{verdict.criterion_id}' is graded; its expected verdict must "
                    "set 'score', not 'met'."
                )
        return self

    def expected_raw(self, criterion_id: str) -> bool | float:
        """Return the human's raw verdict (bool for binary, float for graded)."""
        for verdict in self.expected:
            if verdict.criterion_id == criterion_id:
                return verdict.met if verdict.met is not None else verdict.score  # type: ignore[return-value]
        raise KeyError(
            f"No expected verdict for criterion {criterion_id!r} in fixture {self.id!r}."
        )

    def workspace_path(self, fixture_file: Path) -> Path | None:
        """Resolve ``workspace`` relative to the fixture file's directory."""
        if self.workspace is None:
            return None
        return (fixture_file.parent / self.workspace).resolve()


def load_fixture(path: Path) -> GoldenFixture:
    """Load and validate one golden fixture from a YAML/JSON file (fail loud)."""
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Fixture {path} must be a mapping at the top level.")
    return GoldenFixture(**data)


def load_fixtures(paths: list[Path]) -> list[tuple[Path, GoldenFixture]]:
    """Load every fixture path, keeping the source path for workspace resolution.

    Expands directories to their ``*.yaml`` / ``*.yml`` children. Fails loud if a
    path matches nothing or two fixtures share an ``id`` (ambiguous report rows).
    """
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml")))
        elif path.exists():
            files.append(path)
        else:
            raise FileNotFoundError(f"Fixture path does not exist: {path}")

    if not files:
        raise ValueError(f"No fixture files found under: {[str(p) for p in paths]}")

    loaded = [(f, load_fixture(f)) for f in files]
    ids = [fx.id for _, fx in loaded]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate fixture ids across files: {sorted(dupes)}.")
    return loaded
