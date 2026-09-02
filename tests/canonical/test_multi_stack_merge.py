"""Multi-stack ``EnvironmentPatch.stacks`` merge under
:func:`tolokaforge.core.project_loader.resolve`.

Locks the ADR-0044 § 3 merge algorithm: task-side stacks override
matching project-side entries by ``stack_id``; task-only entries append
in task-declared order; project-only entries survive in project-declared
order; the task-side ``StackPatch.compose_file`` toggles atomic per-stack
replacement vs deep-merge. The per-stack ``stack_scope`` requirement is
locked here too — the multi-stack path never falls back on the scalar
``requires_per_trial`` heuristic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.project_loader import resolve
from tolokaforge.runner.models import EnvironmentPatch, StackPatch

pytestmark = pytest.mark.canonical


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


def _fixture(name: str) -> Path:
    return _FIXTURES / name


class TestMultiStackMerge:
    """The four merge cases from ADR-0044 § 3."""

    def test_project_only_stack_survives(self) -> None:
        """A project-side stack absent from the task side survives
        unchanged, in project-declared order."""
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="run",
                    runner_service="default",
                ),
                "sidecar": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="task",
                    inputs={"SIDECAR_A": "1"},
                ),
            }
        )
        m = resolve(project_env=project_patch, task_env=None)
        assert m is not None
        assert [decl.stack_id for decl in m.stacks] == ["engine", "sidecar"]
        sidecar = m.stacks[1]
        assert sidecar.stack_scope == "task"
        assert sidecar.inputs == {"SIDECAR_A": "1"}

    def test_task_only_stack_appended(self) -> None:
        """A task-side stack absent from the project side appends in
        task-declared order, after every project-side entry."""
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="run",
                    runner_service="default",
                ),
            }
        )
        task_patch = EnvironmentPatch(
            stacks={
                "trial_stack": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="trial",
                    inputs={"TRIAL_A": "1"},
                ),
            }
        )
        m = resolve(project_env=project_patch, task_env=task_patch)
        assert m is not None
        assert [decl.stack_id for decl in m.stacks] == ["engine", "trial_stack"]
        trial_stack = m.stacks[1]
        assert trial_stack.stack_scope == "trial"
        assert trial_stack.inputs == {"TRIAL_A": "1"}

    def test_task_deep_merges_into_project_stack(self) -> None:
        """Task-side patch without ``compose_file`` deep-merges: ``inputs``
        layer per key over the project's; other non-``None`` sub-fields
        override; the project-side ``compose_file`` survives."""
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="task",
                    runner_service="default",
                    inputs={"A": "project_a", "B": "project_b"},
                ),
            }
        )
        task_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    stack_scope="run",
                    inputs={"B": "task_b", "C": "task_c"},
                ),
            }
        )
        m = resolve(project_env=project_patch, task_env=task_patch)
        assert m is not None
        engine = m.stacks[0]
        assert engine.compose_file == _fixture("safe_two_service.yaml")
        assert engine.stack_scope == "run"
        assert engine.runner_service == "default"
        assert engine.inputs == {"A": "project_a", "B": "task_b", "C": "task_c"}

    def test_task_compose_file_triggers_atomic_per_stack_replacement(self) -> None:
        """Task-side patch with ``compose_file`` replaces the whole
        per-stack entry — the project's ``inputs`` / ``runner_service``
        are cleared, mirroring the scalar-form atomic-replacement rule."""
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="run",
                    runner_service="default",
                    inputs={"A": "project_a"},
                ),
            }
        )
        task_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="trial",
                ),
            }
        )
        m = resolve(project_env=project_patch, task_env=task_patch)
        assert m is not None
        engine = m.stacks[0]
        assert engine.compose_file == _fixture("safe_one_service.yaml")
        assert engine.stack_scope == "trial"
        assert engine.runner_service is None
        assert engine.inputs == {}


class TestMultiStackScopeRequired:
    """The multi-stack path does not infer ``stack_scope`` from services —
    every merged entry MUST carry it, and the resolver raises with the
    concrete ADR-0044 § 3 message when it does not."""

    def test_missing_stack_scope_after_merge_refused(self) -> None:
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                ),
            }
        )
        expected = (
            "stacks.engine.stack_scope is required — the multi-stack path "
            "does not infer scope from services (that heuristic applies "
            "only to the scalar 'stack' form; see ADR-0044 § 3)"
        )
        with pytest.raises(ValueError) as excinfo:
            resolve(project_env=project_patch, task_env=None)
        assert str(excinfo.value) == expected


class TestMultiStackScalarShapeRefusal:
    """Mixing the scalar ``stack.compose_file`` with a ``stacks`` block
    across merge layers is refused — the two representations are aliases
    of the same field per ADR-0044 § 3."""

    def test_project_scalar_task_stacks_refused(self) -> None:
        project_patch = EnvironmentPatch(
            stack=StackPatch(compose_file=_fixture("safe_one_service.yaml"))
        )
        task_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="run",
                )
            }
        )
        with pytest.raises(ValueError, match="project side declares scalar"):
            resolve(project_env=project_patch, task_env=task_patch)

    def test_task_scalar_project_stacks_refused(self) -> None:
        project_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="run",
                )
            }
        )
        task_patch = EnvironmentPatch(
            stack=StackPatch(compose_file=_fixture("safe_one_service.yaml"))
        )
        with pytest.raises(ValueError, match="task side declares scalar"):
            resolve(project_env=project_patch, task_env=task_patch)
