"""Unit tests for :func:`tolokaforge.core.project_loader.resolve` and
the :class:`EnvironmentPatch` shape.

Covers the patch-side (no I/O at construction), the atomic-``stack``
replacement rule, deep-merge over ``stack.inputs``, policy-request
survival, and the legacy flat compose_file / runner_service coercion.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tolokaforge.core.models import EnvironmentManifest, EnvironmentPatch, StackPatch
from tolokaforge.core.project_loader import resolve
from tolokaforge.runner.models import NetworkPolicy, SecurityContext, TaskIsolation

pytestmark = pytest.mark.unit


ENV_FIXTURE = (
    Path(__file__).parent.parent
    / "canonical"
    / "fixtures"
    / "environment_manifest"
    / "safe_one_service.yaml"
)


class TestEnvironmentPatchNoIO:
    """The patch shape must not read the compose file at construction —
    that's the invariant :func:`resolve` relies on to defer disk I/O
    to a single point."""

    def test_patch_accepts_nonexistent_path(self) -> None:
        patch = EnvironmentPatch(stack=StackPatch(compose_file=Path("/nonexistent/compose.yaml")))
        assert patch.stack is not None
        assert patch.stack.compose_file == Path("/nonexistent/compose.yaml")

    def test_patch_all_fields_optional(self) -> None:
        patch = EnvironmentPatch()
        assert patch.stack is None
        assert patch.initial_state is None
        assert patch.network_policy is None
        assert patch.security_context_defaults is None
        assert patch.isolation is None

    def test_stack_patch_all_fields_optional(self) -> None:
        stack = StackPatch()
        assert stack.compose_file is None
        assert stack.runner_service is None
        assert stack.inputs == {}


class TestLegacyFlatShape:
    """Task packs authored before M2.5 carry ``compose_file`` and
    ``runner_service`` at the ``environment_manifest`` top level; the
    patch model normalises them under ``stack`` and emits a
    ``DeprecationWarning``. Retirement lands in M5."""

    def test_flat_compose_file_migrates_into_stack(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            patch = EnvironmentPatch.model_validate(
                {
                    "compose_file": str(ENV_FIXTURE),
                    "runner_service": "default",
                }
            )
        assert patch.stack is not None
        assert patch.stack.compose_file == ENV_FIXTURE
        assert patch.stack.runner_service == "default"
        assert any(
            issubclass(w.category, DeprecationWarning) and "flat compose_file" in str(w.message)
            for w in caught
        )

    def test_flat_and_stack_together_is_a_load_error(self) -> None:
        # A pack that half-migrates would silently drop one side; fail
        # loud instead so the author fixes it.
        with pytest.raises(Exception) as exc:
            EnvironmentPatch.model_validate(
                {
                    "compose_file": str(ENV_FIXTURE),
                    "stack": {"compose_file": str(ENV_FIXTURE)},
                }
            )
        assert "both flat" in str(exc.value)


class TestResolveReturnsNoneWhenBothSidesAreNone:
    def test_both_none(self) -> None:
        assert resolve(None, None) is None


class TestResolveDeepMerges:
    """No atomic-replacement trigger — the merge is a straight
    deep-merge, task fields win on conflict, others inherit."""

    def test_task_inputs_deep_merge_over_project_stack(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(
                compose_file=ENV_FIXTURE,
                runner_service="default",
                inputs={"postgres_version": "16", "keep_me": "yes"},
            ),
        )
        task = EnvironmentPatch(stack=StackPatch(inputs={"postgres_version": "17"}))
        manifest = resolve(project, task)
        assert isinstance(manifest, EnvironmentManifest)
        assert manifest.compose_file == ENV_FIXTURE
        assert manifest.runner_service == "default"
        assert manifest.stack_inputs == {"postgres_version": "17", "keep_me": "yes"}

    def test_task_runner_service_alone_deep_merges(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE, runner_service="default"),
            network_policy=NetworkPolicy.NO_INTERNET,
        )
        task = EnvironmentPatch(stack=StackPatch(runner_service="default"))
        manifest = resolve(project, task)
        assert manifest is not None
        assert manifest.compose_file == ENV_FIXTURE
        assert manifest.runner_service == "default"
        assert manifest.network_policy == NetworkPolicy.NO_INTERNET

    def test_only_project_side(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE, runner_service="default"),
        )
        manifest = resolve(project, None)
        assert manifest is not None
        assert manifest.compose_file == ENV_FIXTURE
        assert manifest.runner_service == "default"

    def test_only_task_side(self) -> None:
        task = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE, runner_service="default"),
        )
        manifest = resolve(None, task)
        assert manifest is not None
        assert manifest.compose_file == ENV_FIXTURE


class TestAtomicStackReplacement:
    """Presence of ``compose_file`` on the task's ``stack`` patch
    triggers full replacement of the project's ``stack``. The trigger
    is key presence, not path identity (a re-declaration of the same
    file still replaces)."""

    def test_task_compose_file_replaces_project_inputs(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(
                compose_file=ENV_FIXTURE,
                runner_service="default",
                inputs={"postgres_version": "16"},
            ),
        )
        task = EnvironmentPatch(stack=StackPatch(compose_file=ENV_FIXTURE))
        manifest = resolve(project, task)
        assert manifest is not None
        # Task's stack replaces project's — inputs reset to empty.
        assert manifest.stack_inputs == {}
        # runner_service falls back to the manifest's own default,
        # not the project's declaration.
        assert manifest.runner_service == "default"

    def test_replacement_discards_project_isolation(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE),
            isolation=TaskIsolation.SHARED_OK,
        )
        task = EnvironmentPatch(stack=StackPatch(compose_file=ENV_FIXTURE))
        manifest = resolve(project, task)
        assert manifest is not None
        assert manifest.isolation == TaskIsolation.PER_TRIAL

    def test_replacement_discards_project_initial_state(self) -> None:
        from tolokaforge.runner.models import InitialStateRef

        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE),
            initial_state={"db": InitialStateRef(**{"from": "./seed.sql", "kind": "sql"})},
        )
        task = EnvironmentPatch(stack=StackPatch(compose_file=ENV_FIXTURE))
        manifest = resolve(project, task)
        assert manifest is not None
        assert manifest.initial_state == {}

    def test_policy_request_survives_replacement(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE),
            network_policy=NetworkPolicy.FULL_INTERNET,
            security_context_defaults=SecurityContext(run_as_user=1000),
        )
        task = EnvironmentPatch(stack=StackPatch(compose_file=ENV_FIXTURE))
        manifest = resolve(project, task)
        assert manifest is not None
        assert manifest.network_policy == NetworkPolicy.FULL_INTERNET
        assert manifest.security_context_defaults is not None
        assert manifest.security_context_defaults.run_as_user == 1000

    def test_task_can_override_policy_request_on_replacement(self) -> None:
        project = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE),
            network_policy=NetworkPolicy.NO_INTERNET,
        )
        task = EnvironmentPatch(
            stack=StackPatch(compose_file=ENV_FIXTURE),
            network_policy=NetworkPolicy.LIMITED_INTERNET,
        )
        manifest = resolve(project, task)
        assert manifest is not None
        assert manifest.network_policy == NetworkPolicy.LIMITED_INTERNET


class TestResolveWithoutComposeFileFailsLoud:
    def test_neither_side_declares_compose_file(self) -> None:
        project = EnvironmentPatch(network_policy=NetworkPolicy.NO_INTERNET)
        task = EnvironmentPatch(isolation=TaskIsolation.PER_TRIAL)
        with pytest.raises(ValueError, match="no compose_file"):
            resolve(project, task)
