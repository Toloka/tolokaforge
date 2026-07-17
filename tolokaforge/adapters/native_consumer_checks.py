"""Consumer-surface validation for native task trees.

A native ``task.yaml``/``grading.yaml`` pair has THREE real production
consumers, each with a different schema appetite:

1. **Adapter load** — :func:`tolokaforge.adapters._task_loader.load_task_yaml`
   via :meth:`NativeAdapter.get_task` (task discovery, shared-domain merge,
   ``TaskConfig`` validation).
2. **Core grading parse** — :meth:`NativeAdapter.get_grading_config` parses
   the core :class:`tolokaforge.core.models.GradingConfig`, which REQUIRES
   ``combine``. Artifact persistence calls this at run end.
3. **Runner translation** — :meth:`NativeAdapter.to_task_description`
   translates everything into ``tolokaforge.runner.models`` for trial
   registration. Optional core fields become required here (e.g.
   ``llm_judge.model_ref``) and the adapter SILENTLY OMITS components it
   cannot translate, so a positively weighted component can vanish without
   any error.

A task tree that satisfies only some surfaces passes authoring and golden
replay, then dies (or silently loses grading power) downstream — the D16
class: a ``grading.yaml`` with ``state_checks`` but no ``combine`` replayed
cleanly, then trial registration/persistence rejected it with zero authored
diagnosis.

This module exercises all three surfaces with the REAL production functions
(never reimplementations) and additionally checks **active-weight survival**:
every positively weighted grading component declared in the source YAML must
exist and be evaluable in the translated runner config.

Converge-shaped contract: :func:`check_consumer_surfaces` NEVER raises —
every internal exception folds into the returned findings. Raise is reserved
for operators, not authors.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tolokaforge.runner.models import GradingConfig as RunnerGradingConfig

#: Component names the runner's combine step actually reads weights for.
RUNNER_GRADING_COMPONENTS = ("state_checks", "transcript_rules", "llm_judge")

ConsumerSurfaceName = Literal["adapter_load", "core_grading_config", "runner_translation"]


class ConsumerSurfaceFinding(BaseModel):
    """Outcome of exercising one real consumer surface."""

    surface: ConsumerSurfaceName
    passed: bool
    detail: str = ""


class GradingComponentSurvival(BaseModel):
    """Fate of one positively weighted grading component across translation."""

    component: str
    weight: float
    declared_in: Literal["source_combine", "runner_default"]
    present: bool
    evaluable: bool
    reason: str = ""

    @computed_field
    @property
    def survived(self) -> bool:
        return self.present and self.evaluable


class NativeConsumerReport(BaseModel):
    """All three consumer surfaces plus active-weight survival for one task."""

    task_file: str
    surfaces: list[ConsumerSurfaceFinding] = Field(default_factory=list)
    components: list[GradingComponentSurvival] = Field(default_factory=list)

    @computed_field
    @property
    def passed(self) -> bool:
        return all(surface.passed for surface in self.surfaces) and all(
            component.survived for component in self.components
        )

    def failed_surfaces(self) -> list[ConsumerSurfaceFinding]:
        return [surface for surface in self.surfaces if not surface.passed]

    def dropped_components(self) -> list[GradingComponentSurvival]:
        return [component for component in self.components if not component.survived]


def check_consumer_surfaces(task_file: Path) -> NativeConsumerReport:
    """Exercise all three consumer surfaces for one native task. Never raises."""
    from tolokaforge.adapters.native import NativeAdapter

    task_file = Path(task_file)
    report = NativeConsumerReport(task_file=str(task_file))

    # Surface 1: the adapter load path (discovery + shared-domain merge +
    # TaskConfig validation) — the exact production entry point.
    task = None
    task_dir: Path | None = None
    adapter = None
    task_id: str | None = None
    try:
        adapter = NativeAdapter(
            {
                "base_dir": str(task_file.parent),
                "tasks_glob": task_file.name,
            }
        )
        task_ids = adapter.get_task_ids()
        if len(task_ids) != 1:
            raise RuntimeError(
                f"expected exactly one discoverable task at {task_file}, got {task_ids!r}"
            )
        task_id = task_ids[0]
        task = adapter.get_task(task_id)
        task_dir = adapter.get_task_dir(task_id)
        report.surfaces.append(
            ConsumerSurfaceFinding(
                surface="adapter_load",
                passed=True,
                detail=f"get_task({task_id!r}) validated TaskConfig",
            )
        )
    except Exception as exc:
        report.surfaces.append(
            ConsumerSurfaceFinding(surface="adapter_load", passed=False, detail=_fmt(exc))
        )
        skip = "skipped: adapter load failed"
        report.surfaces.append(
            ConsumerSurfaceFinding(surface="core_grading_config", passed=False, detail=skip)
        )
        report.surfaces.append(
            ConsumerSurfaceFinding(surface="runner_translation", passed=False, detail=skip)
        )
        return report

    # Surface 2: core GradingConfig parse — what artifact persistence runs.
    try:
        adapter.get_grading_config(task_id)
        report.surfaces.append(
            ConsumerSurfaceFinding(
                surface="core_grading_config",
                passed=True,
                detail="core GradingConfig parsed (combine present)",
            )
        )
    except Exception as exc:
        report.surfaces.append(
            ConsumerSurfaceFinding(surface="core_grading_config", passed=False, detail=_fmt(exc))
        )

    # Surface 3: the FULL translation into runner models — what trial
    # registration runs. This is the production to_task_description, not a
    # re-derivation of its rules.
    runner_grading: RunnerGradingConfig | None = None
    try:
        description = adapter.to_task_description(task_id)
        runner_grading = description.grading
        report.surfaces.append(
            ConsumerSurfaceFinding(
                surface="runner_translation",
                passed=True,
                detail="to_task_description produced a runner TaskDescription",
            )
        )
    except Exception as exc:
        report.surfaces.append(
            ConsumerSurfaceFinding(surface="runner_translation", passed=False, detail=_fmt(exc))
        )

    # Active-weight survival closure: every positively weighted component in
    # the source YAML must exist AND be evaluable in the translated config.
    source_grading = _load_source_grading(task.grading, task_dir)
    report.components = _component_survival(source_grading, runner_grading)
    return report


def _load_source_grading(grading_ref: str | None, task_dir: Path | None) -> dict[str, Any]:
    if not grading_ref or task_dir is None:
        return {}
    try:
        payload = yaml.safe_load((task_dir / grading_ref).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _active_weights(
    source_grading: dict[str, Any],
    runner_grading: RunnerGradingConfig | None,
) -> tuple[dict[str, float], Literal["source_combine", "runner_default"]]:
    combine = source_grading.get("combine")
    if isinstance(combine, dict) and isinstance(combine.get("weights"), dict):
        weights = {
            str(name): float(weight)
            for name, weight in combine["weights"].items()
            if isinstance(weight, (int, float)) and weight > 0
        }
        return weights, "source_combine"
    if runner_grading is not None:
        # No combine authored: the runner will default the weights, so the
        # defaulted components are what grading actually rests on.
        weights = {
            str(name): float(weight)
            for name, weight in runner_grading.weights.items()
            if isinstance(weight, (int, float)) and weight > 0
        }
        return weights, "runner_default"
    return {}, "runner_default"


def _component_survival(
    source_grading: dict[str, Any],
    runner_grading: RunnerGradingConfig | None,
) -> list[GradingComponentSurvival]:
    weights, declared_in = _active_weights(source_grading, runner_grading)
    findings: list[GradingComponentSurvival] = []
    for component, weight in sorted(weights.items()):
        if runner_grading is None:
            findings.append(
                GradingComponentSurvival(
                    component=component,
                    weight=weight,
                    declared_in=declared_in,
                    present=False,
                    evaluable=False,
                    reason="runner translation failed; component fate unknown",
                )
            )
            continue
        if component not in RUNNER_GRADING_COMPONENTS:
            findings.append(
                GradingComponentSurvival(
                    component=component,
                    weight=weight,
                    declared_in=declared_in,
                    present=False,
                    evaluable=False,
                    reason=(
                        f"unknown grading component {component!r}: the runner combines "
                        f"only {list(RUNNER_GRADING_COMPONENTS)}"
                    ),
                )
            )
            continue
        findings.append(
            _survival_for(component, weight, declared_in, source_grading, runner_grading)
        )
    return findings


def _survival_for(
    component: str,
    weight: float,
    declared_in: Literal["source_combine", "runner_default"],
    source_grading: dict[str, Any],
    runner_grading: RunnerGradingConfig,
) -> GradingComponentSurvival:
    if component == "state_checks":
        present, evaluable, reason = _state_checks_fate(source_grading, runner_grading)
    elif component == "transcript_rules":
        present, evaluable, reason = _transcript_rules_fate(runner_grading)
    else:
        present, evaluable, reason = _llm_judge_fate(source_grading, runner_grading)
    return GradingComponentSurvival(
        component=component,
        weight=weight,
        declared_in=declared_in,
        present=present,
        evaluable=evaluable,
        reason=reason,
    )


def _state_checks_fate(
    source_grading: dict[str, Any],
    runner_grading: RunnerGradingConfig,
) -> tuple[bool, bool, str]:
    config = runner_grading.state_checks
    if config is None:
        return False, False, "state_checks absent from the translated runner config"
    hash_evaluable = config.hash_enabled and bool(config.golden_actions or config.expected_hash)
    if hash_evaluable or config.jsonpath_checks or config.env_assertions:
        return True, True, "survived: runner has an evaluable state check"
    source_hash = (source_grading.get("state_checks") or {}).get("hash")
    if isinstance(source_hash, dict) and not source_hash.get("enabled", False):
        return (
            True,
            False,
            "state_checks.hash authored without 'enabled: true' — translation drops "
            "golden_actions (hash_enabled=false) so the weighted component evaluates "
            "to nothing",
        )
    return (
        True,
        False,
        "state_checks translated but carries no evaluable hash, jsonpath, or env assertion",
    )


def _transcript_rules_fate(
    runner_grading: RunnerGradingConfig,
) -> tuple[bool, bool, str]:
    config = runner_grading.transcript_rules
    if config is None:
        return False, False, "transcript_rules absent from the translated runner config"
    if (
        config.must_contain
        or config.disallow_regex
        or config.required_actions
        or config.communicate_info
        or config.max_turns is not None
    ):
        return True, True, "survived: runner has evaluable transcript rules"
    return True, False, "transcript_rules translated but every rule list is empty"


def _llm_judge_fate(
    source_grading: dict[str, Any],
    runner_grading: RunnerGradingConfig,
) -> tuple[bool, bool, str]:
    if runner_grading.llm_judge is not None:
        return True, True, "survived: runner judge configured"
    source_judge = source_grading.get("llm_judge")
    if isinstance(source_judge, dict) and not source_judge.get("model_ref"):
        return (
            False,
            False,
            "llm_judge weighted in source but SILENTLY DROPPED in translation: "
            "model_ref missing (optional in core models, required by the runner, "
            "so the adapter omits the judge)",
        )
    return False, False, "llm_judge absent from the translated runner config"


def _fmt(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
