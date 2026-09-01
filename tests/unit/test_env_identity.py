"""Content-addressed environment identity.

:func:`resolve_environment_identity` returns a stable sha256 over the
canonicalised compose bytes, per-stack composition plan, per-service
isolation map, and referenced seed digests. Two equal manifests emit
equal identities; any change to a covered input flips the digest.

Single-stack manifests (`len(env.stacks) <= 1`) emit today's scalar-form
payload byte-identically; multi-stack manifests emit a per-stack list
payload that cannot collide with the legacy shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.core.env_identity import resolve_environment_identity
from tolokaforge.core.models import ResetSpec, ServiceSpec
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import StackDecl

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _manifest(**overrides) -> EnvironmentManifest:
    kwargs = {
        "compose_file": _FIXTURES / "safe_two_service.yaml",
        "services": {
            "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            "default": ServiceSpec(isolation="shared"),
        },
    }
    kwargs.update(overrides)
    return EnvironmentManifest(**kwargs)


class TestFormat:
    def test_digest_shape(self) -> None:
        identity = resolve_environment_identity(_manifest(), {"baseline": "sha256:abc"})
        assert _DIGEST_PATTERN.match(identity)


class TestStability:
    def test_identical_manifests_have_identical_identity(self) -> None:
        m1 = _manifest()
        m2 = _manifest()
        seeds = {"baseline": "sha256:abc"}
        assert resolve_environment_identity(m1, seeds) == resolve_environment_identity(m2, seeds)

    def test_empty_seed_map_matches_empty_dict(self) -> None:
        m = _manifest()
        assert resolve_environment_identity(m, None) == resolve_environment_identity(m, {})


class TestSensitivity:
    def test_changes_when_stack_inputs_change(self) -> None:
        seeds = {"baseline": "sha256:abc"}
        a = resolve_environment_identity(_manifest(stack_inputs={"pg": "16"}), seeds)
        b = resolve_environment_identity(_manifest(stack_inputs={"pg": "17"}), seeds)
        assert a != b

    def test_changes_when_service_isolation_changes(self) -> None:
        seeds = {"baseline": "sha256:abc"}
        base = _manifest()
        altered = _manifest(
            services={
                "db": ServiceSpec(isolation="shared"),
                "default": ServiceSpec(isolation="shared"),
            }
        )
        assert resolve_environment_identity(base, seeds) != resolve_environment_identity(
            altered, seeds
        )

    def test_changes_when_seed_digest_changes(self) -> None:
        m = _manifest()
        a = resolve_environment_identity(m, {"baseline": "sha256:aaaa"})
        b = resolve_environment_identity(m, {"baseline": "sha256:bbbb"})
        assert a != b

    def test_changes_when_compose_bytes_change(self, tmp_path: Path) -> None:
        # Two manifests with the same services map but different compose
        # bytes must emit different identities.
        compose_a = tmp_path / "a.yaml"
        compose_a.write_text("services:\n  default:\n    image: postgres:16\n")
        compose_b = tmp_path / "b.yaml"
        compose_b.write_text("services:\n  default:\n    image: postgres:17\n")
        seeds: dict[str, str] = {}
        m_a = EnvironmentManifest(compose_file=compose_a)
        m_b = EnvironmentManifest(compose_file=compose_b)
        assert resolve_environment_identity(m_a, seeds) != resolve_environment_identity(m_b, seeds)


class TestByteParityWithLegacy:
    """A single-stack manifest — whether its `stacks` slot is empty (a
    directly-constructed manifest with just the scalar fields) or holds
    one synthesised :class:`StackDecl` mirroring the scalar mirror — MUST
    emit the same digest today's scalar-form legacy path pinned. ADR-0044
    § 7's HARD byte-parity invariant."""

    _PINNED_SAFE_TWO_SERVICE = (
        "sha256:d480c8952d994d69f46051763aeea51cd3c23350ccfae9eb410f23a8bffe5dbc"
    )

    def test_pinned_digest_for_safe_two_service_fixture(self) -> None:
        seeds = {"baseline": "sha256:abc"}
        assert resolve_environment_identity(_manifest(), seeds) == self._PINNED_SAFE_TWO_SERVICE

    def test_synthesised_single_stack_matches_scalar_form(self) -> None:
        seeds = {"baseline": "sha256:abc"}
        scalar_form = _manifest()
        mirror = _manifest(
            stacks=[
                StackDecl(
                    stack_id="default",
                    compose_file=_FIXTURES / "safe_two_service.yaml",
                    stack_scope="trial",
                    runner_service="default",
                    inputs={},
                )
            ],
        )
        assert resolve_environment_identity(mirror, seeds) == resolve_environment_identity(
            scalar_form, seeds
        )
        assert resolve_environment_identity(mirror, seeds) == self._PINNED_SAFE_TWO_SERVICE


def _multi_stack_manifest(
    *,
    tmp_path: Path,
    engine_scope: str = "run",
    task_scope: str = "trial",
    engine_inputs: dict[str, str] | None = None,
    task_inputs: dict[str, str] | None = None,
    engine_runner_service: str = "engine",
    task_runner_service: str | None = None,
    engine_compose_body: str = "services:\n  engine:\n    image: engine:1.0\n",
    task_compose_body: str = "services:\n  workspace:\n    image: workspace:1.0\n",
    stack_order: tuple[str, str] = ("engine", "task"),
) -> EnvironmentManifest:
    engine_compose = tmp_path / f"{engine_scope}_engine.yaml"
    engine_compose.write_text(engine_compose_body)
    task_compose = tmp_path / f"{task_scope}_task.yaml"
    task_compose.write_text(task_compose_body)
    decls = {
        "engine": StackDecl(
            stack_id="engine",
            compose_file=engine_compose,
            stack_scope=engine_scope,
            runner_service=engine_runner_service,
            inputs=dict(engine_inputs or {}),
        ),
        "task": StackDecl(
            stack_id="task",
            compose_file=task_compose,
            stack_scope=task_scope,
            runner_service=task_runner_service,
            inputs=dict(task_inputs or {}),
        ),
    }
    # The scalar mirror on the manifest still needs to point at a valid
    # compose file — mirror the engine stack (which owns the runner).
    return EnvironmentManifest(
        compose_file=engine_compose,
        runner_service=engine_runner_service,
        stacks=[decls[stack_order[0]], decls[stack_order[1]]],
    )


class TestMultiStackDigest:
    """A manifest with two or more :class:`StackDecl` entries emits a
    per-stack payload distinct from the legacy scalar shape. Every stack
    attribute the shape mixes in — plan order, ``stack_scope``,
    ``runner_service``, ``inputs``, canonical compose bytes — flips the
    digest when it changes."""

    def test_same_plan_produces_equal_digest(self, tmp_path: Path) -> None:
        m1_dir = tmp_path / "m1"
        m1_dir.mkdir()
        m2_dir = tmp_path / "m2"
        m2_dir.mkdir()
        m1 = _multi_stack_manifest(tmp_path=m1_dir)
        m2 = _multi_stack_manifest(tmp_path=m2_dir)
        assert resolve_environment_identity(m1) == resolve_environment_identity(m2)

    def test_stack_order_is_significant(self, tmp_path: Path) -> None:
        ordered_dir = tmp_path / "ordered"
        ordered_dir.mkdir()
        swapped_dir = tmp_path / "swapped"
        swapped_dir.mkdir()
        ordered = _multi_stack_manifest(tmp_path=ordered_dir, stack_order=("engine", "task"))
        swapped = _multi_stack_manifest(tmp_path=swapped_dir, stack_order=("task", "engine"))
        assert resolve_environment_identity(ordered) != resolve_environment_identity(swapped)

    def test_changing_stack_scope_flips_digest(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        base = _multi_stack_manifest(tmp_path=base_dir)
        # Flip the task stack from trial -> task; every other input stays equal.
        alt = _multi_stack_manifest(tmp_path=alt_dir, task_scope="task")
        assert resolve_environment_identity(base) != resolve_environment_identity(alt)

    def test_changing_runner_service_flips_digest(self, tmp_path: Path) -> None:
        # `runner_service` is a per-stack digest input, so flipping which
        # stack owns the runner between two otherwise-identical plans
        # flips the digest. Both plans declare the runner on a compose
        # service the file itself declares (the manifest validator
        # refuses references to undeclared services).
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        two_service_compose = (
            "services:\n  engine:\n    image: engine:1.0\n  worker:\n    image: worker:1.0\n"
        )
        base = _multi_stack_manifest(
            tmp_path=base_dir,
            engine_compose_body=two_service_compose,
            engine_runner_service="engine",
        )
        alt = _multi_stack_manifest(
            tmp_path=alt_dir,
            engine_compose_body=two_service_compose,
            engine_runner_service="worker",
        )
        assert resolve_environment_identity(base) != resolve_environment_identity(alt)

    def test_changing_inputs_flips_digest(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        base = _multi_stack_manifest(tmp_path=base_dir, engine_inputs={"pg": "16"})
        alt = _multi_stack_manifest(tmp_path=alt_dir, engine_inputs={"pg": "17"})
        assert resolve_environment_identity(base) != resolve_environment_identity(alt)

    def test_changing_compose_bytes_flips_digest(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        base = _multi_stack_manifest(tmp_path=base_dir)
        alt = _multi_stack_manifest(
            tmp_path=alt_dir,
            engine_compose_body="services:\n  engine:\n    image: engine:2.0\n",
        )
        assert resolve_environment_identity(base) != resolve_environment_identity(alt)

    def test_changing_stack_id_flips_digest(self, tmp_path: Path) -> None:
        # `stack_id` is a per-stack digest key. Rename one stack (nothing
        # else changes) and the digest flips.
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        base = _multi_stack_manifest(tmp_path=base_dir)
        alt = _multi_stack_manifest(tmp_path=alt_dir)
        # Swap the second stack's stack_id from "task" to "workspace" —
        # a rename with identical compose bytes / scope / runner / inputs.
        renamed = StackDecl(
            stack_id="workspace",
            compose_file=alt.stacks[1].compose_file,
            stack_scope=alt.stacks[1].stack_scope,
            runner_service=alt.stacks[1].runner_service,
            inputs=dict(alt.stacks[1].inputs),
        )
        alt.stacks[1] = renamed
        assert resolve_environment_identity(base) != resolve_environment_identity(alt)

    def test_multi_stack_shape_cannot_collide_with_single_stack(self, tmp_path: Path) -> None:
        # A two-stack manifest and a one-stack manifest are structurally
        # distinct — the multi-stack payload carries a top-level `stacks`
        # key that the single-stack (legacy) shape never emits, so the two
        # cannot digest-collide even by malicious construction.
        multi_dir = tmp_path / "multi"
        multi_dir.mkdir()
        multi = _multi_stack_manifest(tmp_path=multi_dir)
        single_compose = tmp_path / "single.yaml"
        single_compose.write_text("services:\n  default:\n    image: engine:1.0\n")
        single = EnvironmentManifest(compose_file=single_compose)
        assert resolve_environment_identity(multi) != resolve_environment_identity(single)
