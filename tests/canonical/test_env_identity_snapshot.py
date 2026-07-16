"""Golden snapshot of the resolved ``environment`` block written into
``env.yaml`` for a manifest-driven (Project-layer / multi-container) trial.

The conductor records ``describe_environment_identity(manifest).model_dump()``
under ``final_env_state["environment"]``. This snapshot pins that serialised
shape against a fixture stack mirroring ``multi_service_lot_ops``: pinned
images, a DSN carrying an embedded password, a plaintext-secret env, ``ro``
bind mounts, and a reset-seed service. The snapshot is the recurrence guard
that DSN passwords are redacted, host mount sources are dropped, and every
required identity piece (images, network policy, DSNs, mounts) is present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.env_identity import describe_environment_identity
from tolokaforge.core.models import ResetSpec, ServiceSpec
from tolokaforge.core.trial import EnvironmentManifest

pytestmark = pytest.mark.canonical

FIXTURE = (
    Path(__file__).parent / "fixtures" / "environment_manifest" / "identity_multi_service.yaml"
)


def _manifest() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=FIXTURE,
        runner_service="runner",
        services={
            "runner": ServiceSpec(isolation="shared"),
            "app-service": ServiceSpec(isolation="shared"),
            "app-db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
        },
    )


def test_environment_block_snapshot(canon_snapshot) -> None:
    """Snapshot the exact ``environment`` mapping the conductor writes."""
    block = describe_environment_identity(_manifest()).model_dump(mode="json")
    canon_snapshot("env_identity").assert_match(block, "multi_service.json")


def test_required_identity_pieces_present() -> None:
    block = describe_environment_identity(_manifest()).model_dump(mode="json")

    assert block["network_policy"] == "no_internet"
    assert block["runner_service"] == "runner"

    app_db = block["services"]["app-db"]
    assert app_db["image"] == "postgres:16"
    assert app_db["pinned"] is True
    assert app_db["isolation"] == "reset"
    assert app_db["reset_seed"] == "baseline"

    app_service = block["services"]["app-service"]
    assert app_service["dsns"] == ["postgresql://app:***@app-db:5432/mfg"]
    assert app_service["mounts"] == ["/srv/app/main.py:ro"]


def test_embedded_secret_never_serialised() -> None:
    block = describe_environment_identity(_manifest()).model_dump(mode="json")
    assert "app_pw" not in json.dumps(block)
