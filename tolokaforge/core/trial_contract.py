"""The versioned JSON-schema contract for the out-of-process trial payloads.

:class:`~tolokaforge.core.trial.TrialNodeInputs` and
:class:`~tolokaforge.core.trial.TrialResult` are the JSON shapes an
out-of-process caller serializes to drive one trial — ``tolokaforge run-trial
--inputs`` on a single box, or a trial shipped to a node. Both models forbid
unknown fields, so a caller that builds the JSON needs a schema to pin to.

:data:`TRIAL_CONTRACT_VERSION` versions those two JSON shapes. It is independent
of ``NODE_PROTOCOL_VERSION`` (which versions the gRPC ``RunTrial`` envelope) and
of the package version. The committed schema files under ``docs/schemas/`` are
the pinned contract; a canonical test holds them level with the models, so a
change to either model reds the test until the schema is regenerated and this
version is deliberately bumped. Regenerate with::

    uv run python scripts/export_trial_contract_schemas.py
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tolokaforge.core.trial import TrialNodeInputs, TrialResult

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = [
    "CONTRACT_MODELS",
    "TRIAL_CONTRACT_VERSION",
    "contract_schema_json",
    "schema_filename",
]

TRIAL_CONTRACT_VERSION = 1

CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "trial_node_inputs": TrialNodeInputs,
    "trial_result": TrialResult,
}
"""The contract's models keyed by their schema-file stem."""


def schema_filename(name: str) -> str:
    """The committed schema file name for contract model *name* at the current version."""
    return f"{name}.v{TRIAL_CONTRACT_VERSION}.json"


def contract_schema_json(model: type[BaseModel]) -> str:
    """The committed on-disk form of *model*'s JSON schema.

    Stable key order and a trailing newline, so the regen script and the ledger
    test produce byte-identical files.
    """
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
