"""Both native read sites refuse a malformed grading shape, in the gate's own sentence.

``NativeAdapter`` reads a ``grading.yaml`` twice on different errands —
:meth:`to_task_description` lowers it onto the wire, :meth:`get_grading_config`
constructs the host-side config — and each answers a value that is neither a mapping
nor absent with the sentence naming the file and the key. ``tolokaforge validate``
answers with the same one; its rows live in
``tests/unit/dx/test_validate_grading_migrations.py``.

**The falsy shapes are the load-bearing rows.** Every read site here is written around
a truthiness test, so a gate mirroring that would answer ``[{enabled: true}]`` and let
``[]`` through — and the falsy family is the expensive one: it reaches the wire as an
absent block, so the pack builds a description that grades that component as nothing
and the trial is paid for before anything notices.

**One tier below those keys, and both errands refuse it.** ``state_checks.hash`` is a block
inside ``state_checks`` rather than a grading key, so the shape gate above never walks it —
and each errand constructs it for itself, ``to_task_description`` reading the file without
``get_grading_config`` ever having run. A key the block does not declare requests nothing, so
dropping it silently at either read grades the component as absent and scores the pack
*higher* than the same block spelled correctly.

**One tier below those keys again, and one errand refuses it.** ``state_checks.hash.golden_actions``
is the list of actions a golden replay executes, and :meth:`to_task_description` is the read
that lowers each action onto the wire — so it is the one that refuses a shape it cannot
lower. :meth:`get_grading_config` hands the block's unclaimed ``golden_actions`` on to the
core engine, which refuses the same shape at its own read
(``tests/unit/grading/test_state_checks_composition.py``), and :meth:`compute_golden_hash`
resolves no source at all and answers ``None`` for every shape (#836). So the rows
below are not parametrised over the errands. A *falsy* source is no replay rather than a
malformed one and loads as no actions to replay; only a truthy value that is not a list is
refused.

Each row drives a real pack on disk through the real adapter, because the shape a read
site is handed is what the loader made of the file, not what a monkeypatch says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.utils.golden_source_shapes import (
    elements_that_are_no_action,
    sources_no_replay_can_iterate,
)
from tolokaforge.adapters._task_loader import load_task
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.golden_replay import (
    UnreplayableGoldenSource,
    UnresolvableGoldenAction,
)
from tolokaforge.core.run_trial import _build_single_task_adapter

pytestmark = pytest.mark.unit


_TASK_ID = "shape_pack"

_READ_SITES = ("to_task_description", "get_grading_config")
"""The two errands ``NativeAdapter`` reads a ``grading.yaml`` on, by method name."""

_GRADING_KEYS = (
    "combine",
    "state_checks",
    "transcript_rules",
    "trace_checks",
    "llm_judge",
    "custom_checks",
)
"""Every key a ``grading.yaml`` may carry — the registry's key set, spelled out here so
a key silently dropped from it fails these rows too."""

_TRUTHY_SHAPES: tuple[Any, ...] = ([{"enabled": True}], "enabled", 3)
_FALSY_SHAPES: tuple[Any, ...] = ([], "", 0, False)
_NON_MAPPING_SHAPES = _TRUTHY_SHAPES + _FALSY_SHAPES

_NON_MAPPING_DOCUMENTS = ("- enabled\n", "enabled\n", "[]\n", "''\n", "0\n", "false\n")
"""A whole ``grading.yaml`` that is a list, a string, a number or a boolean."""


def _pack(tmp_path: Path, *, grading_yaml: str) -> NativeAdapter:
    """A real native pack whose ``grading.yaml`` is *grading_yaml*, byte for byte.

    Nothing else about the pack is unusual — no MCP server, no enabled tools, a real
    ``initial_state.json`` — so the grading file is the only thing either read site can
    refuse it for.
    """
    task_dir = tmp_path / "tasks" / _TASK_ID
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": _TASK_ID,
                "description": "a pack whose grading shape is the question",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": []}},
                "grading": "grading.yaml",
                "system_prompt": "system_prompt.md",
            }
        )
    )
    (task_dir / "grading.yaml").write_text(grading_yaml)
    return NativeAdapter({"tasks_glob": str(tmp_path / "tasks" / "**" / "task.yaml")})


def _read(adapter: NativeAdapter, site: str) -> Any:
    return getattr(adapter, site)(_TASK_ID)


def _wire_grading(adapter: NativeAdapter) -> Any:
    """The description's grading block alone — the whole description also carries the
    pack's own paths and the build's timestamp, which differ between two packs by
    construction."""
    return _read(adapter, "to_task_description").grading


@pytest.mark.parametrize("site", _READ_SITES)
@pytest.mark.parametrize("shape", _NON_MAPPING_SHAPES)
@pytest.mark.parametrize("key", _GRADING_KEYS)
def test_a_grading_key_that_is_not_a_mapping_is_refused_by_both_read_sites(
    tmp_path: Path, key: str, shape: Any, site: str
) -> None:
    """The refusal names the file, the key and the shape received, on either errand.

    Naming all three is what makes it a shape refusal and not something else: a message
    carrying only the offending field, or only the file, sends an author to guess at
    which of six keys lost its indentation.
    """
    adapter = _pack(tmp_path, grading_yaml=yaml.safe_dump({key: shape}))

    with pytest.raises(RuntimeError) as excinfo:
        _read(adapter, site)

    message = str(excinfo.value)
    assert str(tmp_path / "tasks" / _TASK_ID / "grading.yaml") in message
    assert f"'{key}'" in message
    assert f"got {type(shape).__name__} ({shape!r})" in message


@pytest.mark.parametrize("site", _READ_SITES)
@pytest.mark.parametrize("document", _NON_MAPPING_DOCUMENTS)
def test_a_grading_document_that_is_not_a_mapping_is_refused_by_both_read_sites(
    tmp_path: Path, document: str, site: str
) -> None:
    """A file that declares no keys at all is refused naming the file and its shape.

    The document tier splits on truthiness exactly as the key tier does — a top-level
    ``[]`` is read as a grading block with nothing in it — so it is refused on the same
    grounds and by the same pass.
    """
    adapter = _pack(tmp_path, grading_yaml=document)

    with pytest.raises(RuntimeError) as excinfo:
        _read(adapter, site)

    message = str(excinfo.value)
    assert str(tmp_path / "tasks" / _TASK_ID / "grading.yaml") in message
    assert f"is not a YAML mapping (got {type(yaml.safe_load(document)).__name__})" in message


@pytest.mark.parametrize("key", _GRADING_KEYS)
def test_a_bare_grading_key_is_the_absent_block_at_both_read_sites(
    tmp_path: Path, key: str
) -> None:
    """A key with nothing under it reads exactly as a file that never declared it.

    Boundary in the other direction: ``state_checks:`` written with no value parses to
    ``None``, which every reader already makes the *absent* block of, and 10 of the
    corpus's declared key instances are written that way. So the refusal cannot be a
    bare "not a mapping" test.
    """
    bare = _pack(tmp_path / "bare", grading_yaml=f"{key}:\n")
    undeclared = _pack(tmp_path / "undeclared", grading_yaml="{}\n")

    assert _wire_grading(bare) == _wire_grading(undeclared)
    assert _read(bare, "get_grading_config") == _read(undeclared, "get_grading_config")


def test_an_empty_grading_file_is_answered_by_each_read_site_as_it_was(
    tmp_path: Path,
) -> None:
    """A file with no content is not content of the wrong type, and this gate leaves it.

    ``to_task_description`` builds the same description an empty mapping builds, while
    ``get_grading_config`` raises the bare ``AttributeError`` naming neither file nor
    fix — the tier #879 owns. Pinned here so widening the shape gate to swallow it
    cannot happen quietly: doing so would turn that crash into a pack grading nothing.
    """
    empty = _pack(tmp_path / "empty", grading_yaml="")
    undeclared = _pack(tmp_path / "undeclared", grading_yaml="{}\n")

    assert _wire_grading(empty) == _wire_grading(undeclared)
    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'pop'"):
        _read(empty, "get_grading_config")


@pytest.mark.parametrize("site", _READ_SITES)
def test_a_populated_retired_hash_key_is_refused_at_each_read_site(
    tmp_path: Path, site: str
) -> None:
    """A stored hash stops the pack at whichever read a run reaches first.

    Parametrised over the errands rather than driven through one, because the two share
    a file and not an object: ``tolokaforge run-trial`` runs no grading pre-flight, so
    the description build is the only read a trial started there passes through, and a
    refusal that lived on the other errand alone would let such a trial be paid for. Each
    row builds its own adapter and calls one method, so what it measures is that read's
    refusal and not a neighbour's.

    Both replacements are asserted rather than the message as a whole: naming only the
    shape a refusal task cannot use is the failure this retirement exists to avoid.
    """
    adapter = _pack(
        tmp_path,
        grading_yaml=yaml.safe_dump(
            {"state_checks": {"hash": {"enabled": True, "expected_state_hash": "a" * 64}}}
        ),
    )

    with pytest.raises(ValueError) as excinfo:
        _read(adapter, site)

    message = str(excinfo.value)
    assert str(tmp_path / "tasks" / _TASK_ID / "grading.yaml") in message
    assert "state_checks.hash.expected_state_hash has been retired" in message
    assert "golden_actions" in message
    assert "expect_initial_state" in message


#: Both shape tables are shared with the gate's rows and core's, over a tool name this
#: pack's grading file can carry.
_GOLDEN_SOURCES_NO_REPLAY_CAN_ITERATE = sources_no_replay_can_iterate("place_order")
_ELEMENTS_THAT_ARE_NO_ACTION = elements_that_are_no_action("place_order")

#: Every falsy spelling of the source, ``null`` being what a bare ``golden_actions:``
#: parses to — the shape an author reaches by commenting their actions out. Six here where
#: the gate reads five: the empty list is locked for the gate by a test of its own, while
#: this read has to answer all six identically.
_GOLDEN_SOURCES_THAT_REPLAY_NOTHING = (
    pytest.param(None, id="the_key_carrying_nothing"),
    pytest.param([], id="an_empty_list"),
    pytest.param({}, id="an_empty_mapping"),
    pytest.param("", id="an_empty_string"),
    pytest.param(0, id="zero"),
    pytest.param(False, id="false"),
)


def _replaying_pack(tmp_path: Path, golden_actions: Any, **hash_keys: Any) -> NativeAdapter:
    """A pack whose enabled ``hash`` block declares *golden_actions*, in any shape."""
    return _pack(
        tmp_path,
        grading_yaml=yaml.safe_dump(
            {
                "state_checks": {
                    "hash": {"enabled": True, "golden_actions": golden_actions, **hash_keys}
                }
            }
        ),
    )


@pytest.mark.parametrize(("golden_actions", "kind"), _GOLDEN_SOURCES_NO_REPLAY_CAN_ITERATE)
def test_a_golden_source_no_replay_can_iterate_is_refused_at_the_wire_read(
    tmp_path: Path, golden_actions: Any, kind: str
) -> None:
    """The last surface before a trial is registered, so it is the one that has to say so.

    Iterating the authored value lands a bare ``AttributeError`` / ``TypeError`` on
    whoever asked for a description — the run's pre-flight resolves it before the per-task
    catch (#880), so it aborts the whole run naming neither the pack, the key nor a fix.
    """
    adapter = _replaying_pack(tmp_path, golden_actions)

    with pytest.raises(UnreplayableGoldenSource) as excinfo:
        _read(adapter, "to_task_description")

    message = str(excinfo.value)
    assert str(tmp_path / "tasks" / _TASK_ID / "grading.yaml") in message
    assert "state_checks.hash.golden_actions" in message
    assert f"got {kind} ({golden_actions!r})" in message


@pytest.mark.parametrize("golden_actions", _GOLDEN_SOURCES_THAT_REPLAY_NOTHING)
def test_a_falsy_golden_source_loads_as_no_actions_to_replay(
    tmp_path: Path, golden_actions: Any
) -> None:
    """A source read for truth, which is how every rule and doc in this family reads it.

    The description carries the flag its author wrote and no actions to replay — the flag
    is not coerced off, because the author did ask for hash grading. This is a load-tier
    lock and asserts nothing about the verdict that description later earns: the runner
    grades an empty replay against the trial's initial state where core takes no verdict at
    all, which is #693's asymmetry and untouched here.

    The rows that are not the empty list are the ones a truthiness-mirroring read gets
    wrong: ``golden_actions: null`` is what an author reaches by commenting their actions
    out, and a read whose default fires only for an absent key iterates it.
    """
    state_checks = _wire_grading(_replaying_pack(tmp_path, golden_actions)).state_checks

    assert state_checks.golden_actions == []
    assert state_checks.hash_enabled is True


@pytest.mark.parametrize("element", _ELEMENTS_THAT_ARE_NO_ACTION)
def test_an_action_that_is_no_mapping_is_refused_before_anything_is_built(
    tmp_path: Path, element: Any
) -> None:
    """The index-naming refusal core reaches through resolution, raised here at the read.

    This substrate has no resolution step in front of a paid trial, so tolerating the
    element is not open to it: ``GoldenAction.tool_name`` is a bare ``str``, so a name read
    off a non-mapping as ``""`` **constructs cleanly**, ``RegisterTrial`` accepts the trial,
    and the resolve fails once the trial is paid for. What rules that out is the refusal
    escaping ``to_task_description`` itself — no description is returned, so no caller
    reaches ``RegisterTrial`` — and where ``pytest.raises`` sits below is what locks it.
    """
    adapter = _replaying_pack(tmp_path, [{"name": "place_order"}, element])

    with pytest.raises(UnresolvableGoldenAction) as excinfo:
        adapter.to_task_description(_TASK_ID)

    assert "[1]" in str(excinfo.value)
    assert f"{type(element).__name__} ({element!r})" in str(excinfo.value)


_KEYS_THE_HASH_BLOCK_DOES_NOT_DECLARE = (
    pytest.param({"enalbed": True, "expect_initial_state": True}, ["enalbed"], id="a_typod_flag"),
    pytest.param(
        {"enabled": True, "expect_inital_state": True},
        ["expect_inital_state"],
        id="a_typod_source",
    ),
    pytest.param(
        {"hash_enabled": True, "expected_hash": "aaaa", "hash_weight": 0.5},
        ["expected_hash", "hash_enabled", "hash_weight"],
        id="the_runners_own_flattened_names",
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": True, "weigth": 0.6},
        ["weigth"],
        id="a_typod_weight",
    ),
)
"""Blocks that request *nothing* the author asked for, each with every key that does it.

The flattened row is the sharpest: those are the names the *runner* declares for this
block and the ones an author meets in this repo's own substrate tables, so the block
reads as configured hash grading and lowers as none. All three are named in one raise,
which is what lets an author fix the block in a single pass.
"""


@pytest.mark.parametrize(("block", "offending_keys"), _KEYS_THE_HASH_BLOCK_DOES_NOT_DECLARE)
def test_a_hash_key_the_block_does_not_declare_is_refused_at_the_wire_read(
    tmp_path: Path, block: dict[str, Any], offending_keys: list[str]
) -> None:
    """The description build reads the block on its own, so it has to refuse on its own.

    ``get_grading_config`` constructs ``StateHashConfig`` through ``GradingConfig`` and has
    refused these since the block was typed — but it is never called here, and that is the
    assertion: the two errands share a *file*, not an object, and a description built by
    reading the file a second time reaches ``RegisterTrial`` without the first read ever
    having happened (``run-trial`` runs no grading pre-flight at all). A block dropping the
    key silently here lowers ``hash_enabled=False`` onto the wire, so the trial is paid for
    and grades the component it configured as absent — scoring *higher* than the same block
    spelled correctly.
    """
    adapter = _pack(tmp_path, grading_yaml=yaml.safe_dump({"state_checks": {"hash": block}}))

    with pytest.raises(ValidationError) as excinfo:
        adapter.to_task_description(_TASK_ID)

    errors = excinfo.value.errors()
    assert sorted(error["loc"] for error in errors) == [(key,) for key in offending_keys]
    assert {error["type"] for error in errors} == {"extra_forbidden"}


@pytest.mark.parametrize(
    "hash_block",
    [
        pytest.param(None, id="the_key_carrying_nothing"),
        pytest.param({}, id="an_empty_mapping"),
    ],
)
def test_a_hash_block_declaring_nothing_reaches_the_wire_as_an_absent_one(
    tmp_path: Path, hash_block: Any
) -> None:
    """The positive control for the rows above, and the one shape that must not refuse.

    ``hash:`` written bare, an empty mapping and no ``hash`` key at all are one block on
    the wire. Reading the raw value for *truthiness* rather than for ``None`` would keep
    them equal by swallowing ``hash: 0`` beside them, which the grading config refuses.
    """
    declared = _pack(
        tmp_path / "declared",
        grading_yaml=yaml.safe_dump({"state_checks": {"hash": hash_block, "jsonpaths": []}}),
    )
    absent = _pack(
        tmp_path / "absent", grading_yaml=yaml.safe_dump({"state_checks": {"jsonpaths": []}})
    )

    assert _wire_grading(declared).state_checks == _wire_grading(absent).state_checks


@pytest.mark.parametrize("shape", _TRUTHY_SHAPES + _FALSY_SHAPES)
def test_a_hash_block_that_is_not_a_mapping_is_refused_at_the_wire_read(
    tmp_path: Path, shape: Any
) -> None:
    """One tier below the keys ``refuse_malformed_grading_shapes`` walks, and unwalked by it.

    That gate answers the six top-level grading keys; ``hash`` sits inside ``state_checks``
    and reaches this read whatever its shape. The falsy half is the expensive one — it is
    read as a block requesting no hash at all, so the trial is paid for before anything
    notices — and it is the half a truthiness test cannot answer.
    """
    adapter = _pack(tmp_path, grading_yaml=yaml.safe_dump({"state_checks": {"hash": shape}}))

    with pytest.raises(ValidationError) as excinfo:
        adapter.to_task_description(_TASK_ID)

    assert [(error["loc"], error["type"], error["input"]) for error in excinfo.value.errors()] == [
        ((), "model_type", shape)
    ]


def test_the_run_trial_path_refuses_a_falsy_grading_shape(tmp_path: Path) -> None:
    """``tolokaforge run-trial`` runs no grading pre-flight, so the read site is the gate.

    ``run_trial`` builds a single-task adapter and asks it for a description before any
    backend, client or grader exists — and nothing upstream of that has looked at the
    grading file. A ``transcript_rules: []`` reaching the wire there would grade that
    component as nothing with no other surface to say so, which is why this path gets
    its own row rather than being implied by the rows above.
    """
    _pack(tmp_path, grading_yaml=yaml.safe_dump({"transcript_rules": []}))
    task = load_task(tmp_path / "tasks" / _TASK_ID / "task.yaml")

    adapter = _build_single_task_adapter(task)

    with pytest.raises(RuntimeError) as excinfo:
        adapter.to_task_description(task.task_id)

    assert "'transcript_rules'" in str(excinfo.value)


_A_REQUIRED_ACTION: dict[str, Any] = {
    "action_id": "cancel_the_order",
    "requestor": "assistant",
    "name": "cancel_order",
    "arguments": {"order_id": "O1"},
}
"""One well-formed ``required_actions`` element, in the spelling an author writes."""


def _transcript_pack(tmp_path: Path, required_actions: list[Any]) -> NativeAdapter:
    return _pack(
        tmp_path,
        grading_yaml=yaml.safe_dump({"transcript_rules": {"required_actions": required_actions}}),
    )


def test_a_required_action_reaches_the_wire_under_the_name_its_author_wrote(
    tmp_path: Path,
) -> None:
    """One model serves the block and the wire, so the read validates instead of copying.

    The positive control for the rows below: without it, a read that dropped
    ``required_actions`` on the floor entirely would satisfy every rejection here.
    """
    action = _wire_grading(_transcript_pack(tmp_path, [_A_REQUIRED_ACTION])).transcript_rules
    assert [a.name for a in action.required_actions] == ["cancel_order"]
    assert [a.action_id for a in action.required_actions] == ["cancel_the_order"]


@pytest.mark.parametrize("omitted", ["name", "action_id", "requestor"])
def test_a_required_action_missing_a_field_it_must_declare_is_refused_at_the_wire_read(
    tmp_path: Path, omitted: str
) -> None:
    """A field the read once substituted a default for now fails before the trial is paid for.

    Each of these was read off the raw mapping with a fallback — ``""`` for the tool and
    the id, ``"user"`` for the requestor — so a pack omitting one registered cleanly and
    graded a required action nothing could satisfy, or one whose requestor the author
    never wrote.
    """
    element = {key: value for key, value in _A_REQUIRED_ACTION.items() if key != omitted}
    adapter = _transcript_pack(tmp_path, [element])

    with pytest.raises(ValidationError) as excinfo:
        adapter.to_task_description(_TASK_ID)

    assert [error["loc"] for error in excinfo.value.errors()] == [("required_actions", 0, omitted)]
