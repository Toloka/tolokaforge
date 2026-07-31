"""How a hash verdict and a JSONPath score fold into one ``state_checks`` score.

Substrate-neutral by construction: pure functions over floats, so both the core
grading engine and the runner service compose the component by the same rule
instead of each carrying its own arithmetic. Kept out of ``state_checks`` because
that module pulls ``importlib.util``, ``jsonpath_ng`` and the diff formatter,
none of which the runner has any reason to import.
"""

from __future__ import annotations

MISSING_HASH_WEIGHT_MESSAGE = (
    "state_checks.hash.weight is required when a hash source and a non-empty "
    "state_checks.jsonpaths are both configured — there is no defensible default. "
    "Choose one: weight: 1.0 lets the hash decide, weight: 0.0 lets the jsonpaths "
    "decide, weight: 0.5 gives them equal shares."
)

_WEIGHT_DOMAIN = "state_checks.hash.weight must be a real number within [0.0, 1.0]"


def validate_hash_weight(value: object, *, context: str) -> float:
    """Return ``value`` as a weight, or raise ``ValueError`` naming ``context``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{context}: {_WEIGHT_DOMAIN}, got {value!r} of type {type(value).__name__}"
        )
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{context}: {_WEIGHT_DOMAIN}, got {value!r}")
    return float(value)


def compose_state_checks_score(
    *,
    hash_score: float | None,
    jsonpath_score: float | None,
    hash_weight: float | None,
) -> float | None:
    """Fold the two state-check sources into one component score.

    ``None`` means *not evaluated*, on input and on output: a source that was
    not configured contributes nothing rather than a vacuous ``1.0`` or a
    failing-looking ``0.0``. A single evaluated source is therefore passed
    through untouched and ``hash_weight`` is never consulted — which is what
    keeps a tau-style pack (hash on, no jsonpaths) scoring its hash verdict at
    every weight. The weight is consulted only when both sources are real, and
    then it is mandatory: every candidate default silently discards one of them.
    """
    if hash_score is None and jsonpath_score is None:
        return None
    if hash_score is None:
        return jsonpath_score
    if jsonpath_score is None:
        return hash_score
    if hash_weight is None:
        raise ValueError(MISSING_HASH_WEIGHT_MESSAGE)
    return jsonpath_score * (1.0 - hash_weight) + hash_score * hash_weight
