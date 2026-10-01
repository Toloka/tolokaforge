"""The db-service image's own stable hash agrees with core's, byte for byte.

The db-service image ships ``tolokaforge/env/json_db_service/`` without the engine wheel,
so ``app.py`` cannot import :mod:`tolokaforge.core.hash` and runs the
``compute_stable_hash`` and ``filter_unstable_fields`` its ``except ImportError`` branch
vendors. That copy is the production path of every stable hash the service serves — the
server-side hash verdict, ETags, snapshot digests — so nothing but this module holds it
to core. The properties below draw nested states and compare the two digests through
the loader the table-name resolution's parity test uses
(:func:`tests.utils.db_service_fallback.load_standalone_fallback`), which blocks the core
import while ``app.py`` loads, so the code that runs here is the image's.

#1717 is the structural fix: the image imports one dependency-free implementation and
the vendored copy goes, leaving this as a one-implementation check.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.utils.db_service_fallback import load_standalone_fallback
from tolokaforge.core import hash as core_hash

pytestmark = pytest.mark.unit

_HASH = load_standalone_fallback("compute_stable_hash")
_FILTER = load_standalone_fallback("filter_unstable_fields")


def test_the_copies_under_test_are_the_db_services_own() -> None:
    """The loader handed over the vendored functions, not core's under another name."""
    for vendored, shared in (
        (_HASH, core_hash.compute_stable_hash),
        (_FILTER, core_hash.filter_unstable_fields),
    ):
        assert vendored is not shared
        source = Path(inspect.getsourcefile(vendored) or "")
        assert source.parts[-2:] == ("json_db_service", "app.py"), source


# ---------------------------------------------------------------------------
# Strategies: every value shape the two numeric folds and the filter branch on
# ---------------------------------------------------------------------------

#: Record keys the declarations below name, so the per-field string fold and the
#: unstable filter meet the keys they act on, beside arbitrary unicode keys.
_NAMED_KEYS = ("id", "amount", "qty", "code", "meta", "rows", "orders")

_KEYS = st.sampled_from(_NAMED_KEYS) | st.text(max_size=6)

_NUMERIC_STRINGS = st.from_regex(r"[+-]?(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?", fullmatch=True)

_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=-(2**53), max_value=2**53).map(float),
    st.floats(),
    st.decimals(),
    _NUMERIC_STRINGS,
    _NUMERIC_STRINGS.map(lambda s: f" {s} "),
    st.sampled_from(["007", "-0", "+5", ".5", "5.", "1e3", "0.0", "\x00tf-num:5", "\x00x"]),
    st.text(max_size=8),
    st.datetimes(timezones=st.none() | st.just(timezone.utc)),
)

_VALUES = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_KEYS, children, max_size=4),
        st.tuples(children, children),
        st.sets(st.text(max_size=3), max_size=3),
    ),
    max_leaves=24,
)

_RECORDS = st.lists(st.dictionaries(_KEYS, _VALUES, max_size=5), max_size=4)

_STATES = st.dictionaries(_KEYS, _RECORDS | _VALUES, max_size=4)

_UNSTABLE = st.none() | st.lists(
    st.sampled_from(
        [f"{table}.{field}" for table in ("rows", "orders", "meta") for field in _NAMED_KEYS]
        + list(_NAMED_KEYS)
        + ["rows.meta.id"]
    ),
    max_size=4,
)

_STRING_FIELDS = st.none() | st.lists(st.sampled_from(_NAMED_KEYS), max_size=3)


def _outcome(function: Any, *args: Any, **kwargs: Any) -> tuple[str, Any]:
    """What ``function`` returned, or the type of what it raised: both copies must agree."""
    try:
        return "returned", function(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the raised type is the datum compared
        return "raised", type(exc)


# ---------------------------------------------------------------------------
# The production call shapes
# ---------------------------------------------------------------------------


@given(_STATES, _UNSTABLE, _STRING_FIELDS, st.booleans())
@settings(max_examples=500, deadline=None)
def test_the_vendored_stable_hash_is_cores_byte_for_byte(
    state: dict[str, Any],
    unstable_fields: list[str] | None,
    numeric_string_fields: list[str] | None,
    canonicalize_numbers: bool,
) -> None:
    """``compute_stable_hash(data, unstable, numeric_string_fields=...)``, as the service calls it."""
    kwargs = {
        "canonicalize_numbers": canonicalize_numbers,
        "numeric_string_fields": numeric_string_fields,
    }
    vendored = _outcome(_HASH, state, unstable_fields, **kwargs)
    shared = _outcome(core_hash.compute_stable_hash, state, unstable_fields, **kwargs)
    assert vendored == shared


@given(_STATES)
@settings(max_examples=300, deadline=None)
def test_the_full_state_hash_is_cores_byte_for_byte(state: dict[str, Any]) -> None:
    """``compute_stable_hash(data)``: the full hash the service reports beside the stable one."""
    assert _outcome(_HASH, state) == _outcome(core_hash.compute_stable_hash, state)


@given(_STATES, _UNSTABLE)
@settings(max_examples=300, deadline=None)
def test_the_vendored_filter_is_cores(
    state: dict[str, Any], unstable_fields: list[str] | None
) -> None:
    """``get_stable_state`` returns this filter's output, which a client-side hash reads."""
    assert _outcome(_FILTER, state, unstable_fields) == _outcome(
        core_hash.filter_unstable_fields, state, unstable_fields
    )


@pytest.mark.parametrize(
    ("state", "string_fields"),
    [
        pytest.param({"t": [{"id": 5}, {"id": 5.0}]}, None, id="integral-float"),
        pytest.param({"t": [{"x": -0.0}, {"x": Decimal("0.00")}]}, None, id="negative-zero"),
        pytest.param({"t": [{"x": 10**23}, {"x": 1e23}]}, None, id="large-integral-float"),
        pytest.param({"t": [{"amount": "130.00"}, {"amount": " 130 "}]}, ["amount"], id="money"),
        pytest.param({"t": [{"code": "007"}, {"code": "1.10"}]}, ["code"], id="id-like-string"),
        pytest.param({"t": [{"x": "\x00tf-num:5"}, {"x": 5}]}, None, id="reserved-prefix"),
        pytest.param({"ü": [{"ключ": "значение", "数": 1.5}]}, None, id="unicode-keys"),
        pytest.param({"t": [{"a": [[1, 2.0], {"b": None}]}]}, None, id="nested-lists"),
        pytest.param({"t": [{"at": datetime(2026, 1, 1, tzinfo=timezone.utc)}]}, None, id="dt"),
        pytest.param({"t": [{"x": float("nan")}, {"y": float("inf")}]}, None, id="non-finite"),
    ],
)
def test_each_fold_hashes_alike_on_both(
    state: dict[str, Any], string_fields: list[str] | None
) -> None:
    for unstable in (None, ["t.id"]):
        assert _HASH(state, unstable, numeric_string_fields=string_fields) == (
            core_hash.compute_stable_hash(state, unstable, numeric_string_fields=string_fields)
        )
