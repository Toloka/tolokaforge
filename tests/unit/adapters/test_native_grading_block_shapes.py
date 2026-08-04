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

Each row drives a real pack on disk through the real adapter, because the shape a read
site is handed is what the loader made of the file, not what a monkeypatch says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task
from tolokaforge.adapters.native import NativeAdapter
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
