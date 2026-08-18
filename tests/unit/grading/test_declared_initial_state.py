"""What each layer of the ``initial_state.json_db`` reader answers, per shape a task wrote.

A task declares the state it starts in either as an inline mapping in ``task.yaml`` or as
the name of a JSON file beside it, and two readings of that declaration exist for two
different callers.

:func:`~tolokaforge.core.grading.golden_replay.read_declared_initial_state` is the
shape-neutral one: the raw mapping out of either shape, ``None`` where the task declared
nothing, and a refusal naming only ``initial_state.json_db``, the declared path and the
problem — so whoever holds the state decides for itself what an absent one means, and the
sentence reads correctly to whichever component surfaces it.
:func:`~tolokaforge.core.grading.golden_replay.resolve_initial_state` is the reading
``state_checks.hash.expect_initial_state`` needs: it hashes what it gets back, so it admits
neither an absent state nor an empty one, and frames every refusal as the hash source's.

The empty state is where the two part, deliberately. A state holding nothing hashes to a
digest no trial can match, so the hash source refuses the file that holds it; the reader
below hands that file back as the empty mapping, like the inline shape whose empty mapping
is no declaration at all. Both halves are pinned here because the layers are only useful
apart if each one's answer to the empty state is the one its caller can act on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.golden_replay import (
    UnresolvableInitialState,
    read_declared_initial_state,
    resolve_initial_state,
)

pytestmark = pytest.mark.unit

_JSON_DB = "initial_state.json"

_DECLARED_STATE: dict[str, Any] = {
    "orders": [{"id": "O-1", "status": "pending"}, {"id": "O-2", "status": "paid"}]
}


def _task_dir(tmp_path: Path, written: str | None) -> Path:
    if written is not None:
        (tmp_path / _JSON_DB).write_text(written)
    return tmp_path


def test_a_path_and_the_inline_mapping_it_holds_read_back_as_one_state(tmp_path) -> None:
    """The two authoring shapes are one declaration, and both layers read the same one.

    The inline leg is asserted against the state itself rather than against the file leg,
    so a reader that answered both shapes with the same wrong thing — the declared string,
    an empty mapping — agrees with itself and still fails. The resolver rides along
    because it is the same reading framed: an implementation that kept a private copy of
    the loading is free to drift from it, and this is where that drift shows.
    """
    task_dir = _task_dir(tmp_path, json.dumps(_DECLARED_STATE))

    from_file = read_declared_initial_state(task_dir=task_dir, initial_state_json_db=_JSON_DB)
    inline = read_declared_initial_state(task_dir=task_dir, initial_state_json_db=_DECLARED_STATE)

    assert from_file == _DECLARED_STATE
    assert inline == _DECLARED_STATE
    assert (
        resolve_initial_state(task_dir=task_dir, initial_state_json_db=_JSON_DB) == _DECLARED_STATE
    )


@pytest.mark.parametrize(
    ("declared", "written", "expected"),
    [
        pytest.param(None, None, None, id="nothing_declared"),
        pytest.param({}, None, None, id="an_inline_mapping_holding_nothing"),
        pytest.param(_JSON_DB, "{}", {}, id="a_file_holding_an_empty_json_object"),
    ],
)
def test_an_undeclared_state_reads_back_as_none_and_a_declared_empty_one_as_itself(
    declared: str | dict[str, Any] | None,
    written: str | None,
    expected: dict[str, Any] | None,
    tmp_path,
) -> None:
    """Absence is a fact the reader reports, and the empty state is a state it hands over.

    ``None`` rather than a raise because a task declaring no initial state is legal
    wherever the state is evidence rather than the thing a verdict is computed from. The
    file holding ``{}`` is the row the empty-state refusal must not be pushed down into:
    an inline mapping holding nothing is already no declaration at all, so a reader that
    refused the file instead of answering ``{}`` would make the two shapes disagree in the
    opposite direction to the divergence this reader exists to close.
    """
    assert (
        read_declared_initial_state(
            task_dir=_task_dir(tmp_path, written), initial_state_json_db=declared
        )
        == expected
    )


@pytest.mark.parametrize(
    ("resolvable_under_a_task_dir", "written", "problem"),
    [
        pytest.param(True, None, "FileNotFoundError", id="a_file_the_task_does_not_carry"),
        pytest.param(True, "[]", "it holds a list", id="a_file_holding_no_json_object"),
        pytest.param(True, "{oops", "JSONDecodeError", id="a_file_holding_no_json_at_all"),
        pytest.param(False, None, "no task directory", id="a_file_and_no_task_dir_to_resolve_it"),
    ],
)
def test_a_declaration_no_reader_can_resolve_names_the_key_the_path_and_the_problem(
    resolvable_under_a_task_dir: bool, written: str | None, problem: str, tmp_path
) -> None:
    """The refusal an author can act on, addressed to nobody in particular.

    Every sentence carries the authored key, the path as the task wrote it — not the
    absolute one a reader resolved, which names a directory the author never chose — and
    what went wrong with it. None carries the hash source's framing: this reader serves
    callers for whom ``state_checks.hash.expect_initial_state`` is not the reason the state
    was wanted, and a refusal wearing that sentence would send them to the wrong key.

    The last row is reachable only through this reader — every caller that resolves a task
    directory of its own has one by then — which is why it is pinned at this layer rather
    than through a component that could never produce it.
    """
    task_dir = _task_dir(tmp_path, written) if resolvable_under_a_task_dir else None

    with pytest.raises(UnresolvableInitialState) as excinfo:
        read_declared_initial_state(task_dir=task_dir, initial_state_json_db=_JSON_DB)

    message = str(excinfo.value)
    assert "initial_state.json_db" in message, message
    assert _JSON_DB in message, message
    assert problem in message, message
    assert "expect_initial_state" not in message, message


@pytest.mark.parametrize(
    ("declared", "written", "problem"),
    [
        pytest.param(None, None, "declares no initial_state.json_db", id="nothing_declared"),
        pytest.param(
            _JSON_DB, "{}", "it holds an empty JSON object", id="a_file_holding_an_empty_state"
        ),
        pytest.param(_JSON_DB, None, "FileNotFoundError", id="a_file_the_task_does_not_carry"),
    ],
)
def test_the_hash_source_frames_every_refusal_as_its_own(
    declared: str | None, written: str | None, problem: str, tmp_path
) -> None:
    """The control on the split: what the layer above adds is a whole contract, not a prefix.

    Two things a caller of the hash source relies on and the reader below deliberately does
    not provide — the sentence naming the source that wanted the state and telling its
    author which key to write or which source to drop, and the refusal of a state holding
    nothing, which would otherwise hash to a digest no trial can match and read as an agent
    failure. Both rows the reader answers without raising are here for that reason.
    """
    with pytest.raises(UnresolvableInitialState) as excinfo:
        resolve_initial_state(task_dir=_task_dir(tmp_path, written), initial_state_json_db=declared)

    message = str(excinfo.value)
    assert "state_checks.hash.expect_initial_state" in message, message
    assert "Write initial_state.json_db in task.yaml, or drop the source" in message, message
    assert problem in message, message
