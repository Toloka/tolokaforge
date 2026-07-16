"""Content-addressed environment identity.

:func:`resolve_environment_identity` returns a stable sha256 over the
canonicalised compose bytes, ``stack_inputs``, per-service isolation
map, and referenced seed digests. Two equal manifests emit equal
identities; any change to a covered input flips the digest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.core.env_identity import resolve_environment_identity
from tolokaforge.core.models import ResetSpec, ServiceSpec
from tolokaforge.core.trial import EnvironmentManifest

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
