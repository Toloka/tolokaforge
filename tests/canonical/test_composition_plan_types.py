"""ADR-0044 composition-plan types — `StackDecl`, `PlanShape`, and the
backward-compat coercion that keeps every pre-ADR-0044 task pack loading
byte-identically.

The composition plan is a first-class field on `EnvironmentManifest` (a
list of :class:`StackDecl` entries each with a lifecycle scope). Manifests
authored against the pre-ADR-0044 scalar `compose_file` surface still
resolve to a valid single-entry plan via
:func:`tolokaforge.core.project_loader.resolve`; this file pins the
classification of every plan shape and the coercion invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import ServiceSpec
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import (
    EnvironmentPatch,
    PlanShape,
    StackDecl,
    StackPatch,
)

pytestmark = pytest.mark.canonical


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


def _fixture(name: str) -> Path:
    return _FIXTURES / name


class TestStackDeclShape:
    """`StackDecl` — one compose file at one scope."""

    def test_minimal_stackdecl_constructs(self) -> None:
        decl = StackDecl(
            stack_id="engine",
            compose_file=_fixture("safe_two_service.yaml"),
            stack_scope="run",
        )
        assert decl.stack_id == "engine"
        assert decl.stack_scope == "run"
        assert decl.runner_service is None
        assert decl.inputs == {}

    def test_stack_scope_vocab_is_closed(self) -> None:
        with pytest.raises(ValidationError, match="Input should be 'run', 'task' or 'trial'"):
            StackDecl(  # type: ignore[call-arg]
                stack_id="engine",
                compose_file=_fixture("safe_two_service.yaml"),
                stack_scope="always",
            )

    def test_extra_fields_refused(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            StackDecl(  # type: ignore[call-arg]
                stack_id="engine",
                compose_file=_fixture("safe_two_service.yaml"),
                stack_scope="run",
                bogus=True,
            )


class TestPlanShapeClassification:
    """`EnvironmentManifest.plan_shape` maps a composition plan to the four
    canonical shapes ADR-0044 § 5 names. Backend selection reads it."""

    def test_empty_stacks_returns_trial_scoped_only(self) -> None:
        """Backward-compat default: a manifest without any resolved plan
        classifies as `TRIAL_SCOPED_ONLY`, matching ADR-0009's
        `requires_per_trial=True` default."""
        m = EnvironmentManifest(compose_file=_fixture("safe_one_service.yaml"))
        assert m.stacks == []
        assert m.plan_shape is PlanShape.TRIAL_SCOPED_ONLY

    def test_single_run_scope(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_one_service.yaml"),
            stacks=[
                StackDecl(
                    stack_id="default",
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="run",
                )
            ],
        )
        assert m.plan_shape is PlanShape.SINGLE_RUN

    def test_single_task_scope(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_one_service.yaml"),
            stacks=[
                StackDecl(
                    stack_id="task",
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="task",
                )
            ],
        )
        assert m.plan_shape is PlanShape.TASK_SCOPED_ONLY

    def test_single_trial_scope(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_one_service.yaml"),
            stacks=[
                StackDecl(
                    stack_id="task",
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="trial",
                )
            ],
        )
        assert m.plan_shape is PlanShape.TRIAL_SCOPED_ONLY

    def test_multi_scope_run_and_trial(self) -> None:
        """The canonical T-Bench balanced-10 shape: engine `run` +
        task `trial`. Classifies as MULTI_SCOPE per ADR-0044."""
        m = EnvironmentManifest(
            compose_file=_fixture("safe_two_service.yaml"),
            stacks=[
                StackDecl(
                    stack_id="engine",
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="run",
                    runner_service="default",
                ),
                StackDecl(
                    stack_id="task",
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="trial",
                ),
            ],
        )
        assert m.plan_shape is PlanShape.MULTI_SCOPE

    def test_multi_scope_task_and_trial(self) -> None:
        m = EnvironmentManifest(
            compose_file=_fixture("safe_two_service.yaml"),
            stacks=[
                StackDecl(
                    stack_id="task_stack",
                    compose_file=_fixture("safe_two_service.yaml"),
                    stack_scope="task",
                ),
                StackDecl(
                    stack_id="trial_stack",
                    compose_file=_fixture("safe_one_service.yaml"),
                    stack_scope="trial",
                ),
            ],
        )
        assert m.plan_shape is PlanShape.MULTI_SCOPE


class TestBackwardCompatCoercion:
    """A manifest loaded from the pre-ADR-0044 scalar surface must resolve
    to a valid single-entry composition plan whose scope is inferred from
    the same `requires_per_trial` derivation today's backend selection uses.
    Exercises `project_loader.resolve()` — the seam every consumer takes —
    so a future refactor cannot silently regress the coercion by removing
    or reordering the internal helper."""

    def test_empty_services_infers_trial_scope(self) -> None:
        """No services declared → `requires_per_trial=True` per ADR-0009 →
        synthesised stack has `stack_scope="trial"`. Matches today's
        routing to `PerTrialRuntimeBackend`."""
        from tolokaforge.core.project_loader import resolve

        patch = EnvironmentPatch(stack=StackPatch(compose_file=_fixture("safe_one_service.yaml")))
        m = resolve(project_env=patch, task_env=None)
        assert m is not None
        assert m.requires_per_trial is True
        assert len(m.stacks) == 1
        assert m.stacks[0].stack_scope == "trial"
        assert m.plan_shape is PlanShape.TRIAL_SCOPED_ONLY

    def test_all_shared_services_infers_run_scope(self) -> None:
        """All `shared` → `requires_per_trial=False` → synthesised stack
        has `stack_scope="run"`. Matches today's routing to
        `SharedStackRuntimeBackend`."""
        from tolokaforge.core.project_loader import resolve

        patch = EnvironmentPatch(
            stack=StackPatch(compose_file=_fixture("safe_one_service.yaml")),
            services={"default": ServiceSpec(isolation="shared")},
        )
        m = resolve(project_env=patch, task_env=None)
        assert m is not None
        assert m.requires_per_trial is False
        assert m.stacks[0].stack_scope == "run"
        assert m.plan_shape is PlanShape.SINGLE_RUN

    def test_mixed_isolation_infers_trial_scope(self) -> None:
        """Mixed `shared` + `reset|ephemeral` → `requires_per_trial=True` →
        synthesised stack has `stack_scope="trial"`. Matches today's
        routing to per_trial when the pre-ADR-0044 surface was used."""
        from tolokaforge.core.project_loader import resolve

        patch = EnvironmentPatch(
            stack=StackPatch(compose_file=_fixture("safe_two_service.yaml")),
            services={
                "default": ServiceSpec(isolation="shared"),
                "db": ServiceSpec(isolation="ephemeral"),
            },
        )
        m = resolve(project_env=patch, task_env=None)
        assert m is not None
        assert m.requires_per_trial is True
        assert m.stacks[0].stack_scope == "trial"

    def test_synthesis_is_idempotent(self) -> None:
        """A manifest whose `stacks` list is already populated MUST NOT
        be overwritten by the coercion — an explicit plan wins. Locks the
        internal invariant of the helper; end-to-end coverage is provided
        by the other tests in this class going through `resolve()`."""
        from tolokaforge.core.project_loader import _synthesise_composition_plan

        explicit_stack = StackDecl(
            stack_id="explicit",
            compose_file=_fixture("safe_one_service.yaml"),
            stack_scope="task",
        )
        m = EnvironmentManifest(
            compose_file=_fixture("safe_one_service.yaml"),
            stacks=[explicit_stack],
        )
        _synthesise_composition_plan(m, {})
        assert m.stacks == [explicit_stack]

    def test_scalar_fields_mirror_synthetic_stack(self) -> None:
        """The scalar-form fields (`compose_file`, `runner_service`,
        `stack_inputs`) mirror the sole synthetic stack — every existing
        consumer keeps working unchanged."""
        from tolokaforge.core.project_loader import resolve

        patch = EnvironmentPatch(
            stack=StackPatch(
                compose_file=_fixture("safe_two_service.yaml"),
                runner_service="default",
                inputs={"FOO": "bar"},
            ),
            services={
                "default": ServiceSpec(isolation="shared"),
                "db": ServiceSpec(isolation="shared"),
            },
        )
        m = resolve(project_env=patch, task_env=None)
        assert m is not None
        assert m.stacks[0].compose_file == m.compose_file
        assert m.stacks[0].runner_service == m.runner_service
        assert m.stacks[0].inputs == m.stack_inputs


class TestEnvironmentPatchStacksBlockRefusal:
    """`EnvironmentPatch` refuses the ambiguous case where both the scalar
    `stack.compose_file` and the plural `stacks` block are set — they are
    aliases of the same field. See ADR-0044 § 3."""

    def test_stack_and_stacks_together_refused(self) -> None:
        with pytest.raises(ValidationError, match="aliases of the same field"):
            EnvironmentPatch(
                stack=StackPatch(compose_file=_fixture("safe_one_service.yaml")),
                stacks={"engine": StackPatch(compose_file=_fixture("safe_one_service.yaml"))},
            )

    def test_stack_alone_is_accepted(self) -> None:
        """Legacy scalar-only patch — the byte-identical backward-compat
        path. No `stacks` block; no refusal."""
        patch = EnvironmentPatch(stack=StackPatch(compose_file=_fixture("safe_one_service.yaml")))
        assert patch.stacks is None

    def test_stacks_block_alone_is_accepted(self) -> None:
        """New composition-plan patch — no scalar `stack.compose_file`.
        The multi-stack merge path is not wired in this ticket; this test
        only pins that the patch construction itself accepts the shape.
        The end-to-end refusal on `resolve` is locked by
        :meth:`test_stacks_block_is_refused_end_to_end`."""
        patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"), stack_scope="run"
                )
            }
        )
        assert patch.stack is None
        assert patch.stacks is not None
        assert "engine" in patch.stacks

    def test_stacks_block_is_refused_end_to_end(self) -> None:
        """The whole point of accepting the `stacks` block at patch level
        is that it must be caught at resolve time, not silently degenerate
        into the scalar coercion path. Locks the guard in the synthesiser —
        a follow-up refactor that removes it would land here."""
        from tolokaforge.core.project_loader import resolve

        project_patch = EnvironmentPatch(
            stack=StackPatch(compose_file=_fixture("safe_one_service.yaml"))
        )
        task_patch = EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"), stack_scope="run"
                )
            }
        )
        with pytest.raises(NotImplementedError, match="multi-stack merge path is not yet wired"):
            resolve(project_env=project_patch, task_env=task_patch)

    def test_stack_without_compose_file_and_stacks_together_accepted(self) -> None:
        """Edge case: `stack` set but ONLY carrying inputs/runner_service
        overrides (no `compose_file`). The refusal targets the ambiguous
        compose-file duplicate, not stack-metadata inheritance."""
        patch = EnvironmentPatch(
            stack=StackPatch(inputs={"FOO": "bar"}),
            stacks={
                "engine": StackPatch(
                    compose_file=_fixture("safe_one_service.yaml"), stack_scope="run"
                )
            },
        )
        assert patch.stack is not None
        assert patch.stack.compose_file is None
        assert patch.stacks is not None


class TestStackPatchScopeField:
    """`StackPatch.stack_scope` — new optional field per ADR-0044 § 3."""

    def test_scope_absent_defaults_to_none(self) -> None:
        patch = StackPatch()
        assert patch.stack_scope is None

    def test_scope_vocab_matches_stackdecl(self) -> None:
        for scope in ("run", "task", "trial"):
            patch = StackPatch(stack_scope=scope)  # type: ignore[arg-type]
            assert patch.stack_scope == scope

    def test_invalid_scope_refused(self) -> None:
        with pytest.raises(ValidationError, match="Input should be 'run', 'task' or 'trial'"):
            StackPatch(stack_scope="always")  # type: ignore[arg-type]
