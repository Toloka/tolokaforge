"""Deterministic recovery-policy oracle (the independent, schema-grounded check).

For every model with a fixtures file under ``policy_fixtures/``, resolve that model's
EFFECTIVE response policy from the shipped presets and assert it recovers each known
provider-corruption shape to the exact expected value - and, crucially, leaves a
legitimate look-alike field untouched (the over-reach negative).

Why this exists: the synthetic capability probes exercise fields the real corruption never
lands on and carry only a hand-written assert (no strict validator behind them), so a fix
that MISSES a real shape or OVER-REACHES can still pass them. This test is name-agnostic -
it runs whatever policy the preset resolves to (the shipped composite today, an auto-resolve
candidate's policy inside the integration pipeline) - so a divergent recovery fails here.

Pure Python: no live model, no Docker, no network. Runs in normal CI as a permanent
regression guard, and the resolve stage can gate on it.
"""

from __future__ import annotations

import copy
import glob
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm.presets import build_capabilities

_FIXTURE_DIR = Path(__file__).parent / "policy_fixtures"


def _load_cases() -> list[tuple[str, str, str, dict[str, Any], dict[str, Any], Any]]:
    """Flatten every fixtures file into (id, provider, name, args, expect, param_types)."""
    out: list[tuple[str, str, str, dict[str, Any], dict[str, Any], Any]] = []
    for path in sorted(glob.glob(str(_FIXTURE_DIR / "*.yaml"))):
        doc = yaml.safe_load(Path(path).read_text()) or {}
        provider = doc.get("provider", "")
        name = doc.get("name", "")
        for case in doc.get("cases", []):
            case_id = f"{Path(path).stem}::{case.get('name', '?')}"
            out.append(
                (
                    case_id,
                    provider,
                    name,
                    case.get("args", {}),
                    case.get("expect", {}),
                    case.get("param_types"),
                )
            )
    return out


_CASES = _load_cases()


@pytest.mark.skipif(not _CASES, reason="no policy fixtures present")
@pytest.mark.parametrize(
    "provider,name,args,expect,param_types",
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_resolved_response_policy_recovers_shape(
    provider: str,
    name: str,
    args: dict[str, Any],
    expect: dict[str, Any],
    param_types: Any,
) -> None:
    """The model's resolved response policy must map ``args`` to exactly ``expect``."""
    policy = build_capabilities(name, provider).response_policy
    got = policy.parse_arguments(copy.deepcopy(args), param_types=param_types)
    assert got == expect
