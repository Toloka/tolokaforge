"""Canonical behaviour lock for the local ``str_replace_editor`` engine (#567).

Exercises :class:`LocalFilesystemEditor` against a real temp directory (no
mocks): the four commands, their fail-loud conditions, path containment, and
the schema the native adapter advertises. Line-numbered output is asserted
byte-for-byte; the compose engine's ``docker exec`` behaviour is covered
separately on a real daemon in the integration tier.
"""

from __future__ import annotations

import pytest

from tolokaforge.adapters._task_loader import _builtin_tool_schemas
from tolokaforge.tools.str_replace_editor import (
    _HEAD_CHARS,
    _MAX_OUTPUT_CHARS,
    _TAIL_CHARS,
    EditorError,
    LocalFilesystemEditor,
)

pytestmark = pytest.mark.canonical


def _editor(base) -> LocalFilesystemEditor:
    return LocalFilesystemEditor(str(base))


# -- view ---------------------------------------------------------------------


def test_view_small_file_is_line_numbered(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    out = _editor(tmp_path).view(str(f))
    assert out == "     1\talpha\n     2\tbeta\n     3\tgamma\n"


def test_view_range_returns_one_indexed_slice(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\nl4\nl5\nl6\n", encoding="utf-8")
    out = _editor(tmp_path).view(str(f), view_range=[3, 5])
    assert out == "     3\tl3\n     4\tl4\n     5\tl5\n"


def test_view_range_negative_one_reads_to_eof(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\n", encoding="utf-8")
    out = _editor(tmp_path).view(str(f), view_range=[2, -1])
    assert out == "     2\tl2\n     3\tl3\n"


def test_view_large_file_is_middle_truncated_with_hint(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("".join(f"line number {i}\n" for i in range(20_000)), encoding="utf-8")
    out = _editor(tmp_path).view(str(f))
    assert "[truncated:" in out
    assert "grep" in out
    assert len(out) <= _MAX_OUTPUT_CHARS + (_MAX_OUTPUT_CHARS - _HEAD_CHARS - _TAIL_CHARS) + 200


def test_view_directory_two_levels_skips_hidden_and_pycache(tmp_path):
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("x", encoding="utf-8")
    deep = sub / "deeper"
    deep.mkdir()
    (deep / "too_deep.txt").write_text("x", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "junk.pyc").write_text("x", encoding="utf-8")

    out = _editor(tmp_path).view(str(tmp_path))

    resolved = tmp_path.resolve()
    assert str(resolved / "visible.txt") in out
    assert str(resolved / "sub") in out
    assert str(resolved / "sub" / "nested.txt") in out  # level 2 present
    assert "deeper" in out  # the level-2 directory itself is listed
    assert str(deep / "too_deep.txt") not in out  # level 3 omitted
    assert str(resolved / ".hidden.txt") not in out
    assert str(resolved / "__pycache__") not in out


# -- create -------------------------------------------------------------------


def test_create_writes_new_file(tmp_path):
    f = tmp_path / "new.txt"
    msg = _editor(tmp_path).create(str(f), "hello\nworld\n")
    assert f.read_text(encoding="utf-8") == "hello\nworld\n"
    assert "created successfully" in msg


def test_create_on_existing_path_fails_loud(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("original\n", encoding="utf-8")
    with pytest.raises(EditorError):
        _editor(tmp_path).create(str(f), "overwrite\n")
    assert f.read_text(encoding="utf-8") == "original\n"


# -- str_replace --------------------------------------------------------------


def test_str_replace_unique_match_is_applied(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("the quick brown fox\n", encoding="utf-8")
    _editor(tmp_path).str_replace(str(f), "quick brown", "slow red")
    assert f.read_text(encoding="utf-8") == "the slow red fox\n"


def test_str_replace_non_unique_fails_loud_with_count(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("foo and foo\n", encoding="utf-8")
    with pytest.raises(EditorError) as exc:
        _editor(tmp_path).str_replace(str(f), "foo", "bar")
    assert "2" in str(exc.value)
    assert f.read_text(encoding="utf-8") == "foo and foo\n"


def test_str_replace_missing_match_fails_loud(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world\n", encoding="utf-8")
    with pytest.raises(EditorError):
        _editor(tmp_path).str_replace(str(f), "absent", "x")


# -- insert -------------------------------------------------------------------


def test_insert_after_line_places_text_at_expected_position(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _editor(tmp_path).insert(str(f), 2, "INSERTED")
    assert f.read_text(encoding="utf-8") == "line1\nline2\nINSERTED\nline3\n"


def test_insert_at_zero_prepends_before_first_line(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    _editor(tmp_path).insert(str(f), 0, "TOP")
    assert f.read_text(encoding="utf-8") == "TOP\na\nb\n"


def test_insert_past_eof_fails_loud(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    with pytest.raises(EditorError):
        _editor(tmp_path).insert(str(f), 99, "X")


# -- path containment ---------------------------------------------------------


def test_symlink_escape_fails_loud(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n", encoding="utf-8")
    link = base / "link.txt"
    link.symlink_to(secret)
    with pytest.raises(EditorError):
        _editor(base).view(str(link))


@pytest.mark.parametrize("escape", ["../secret.txt", "/etc/hostname"])
def test_traversal_and_absolute_escape_fail_loud(tmp_path, escape):
    base = tmp_path / "work"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    target = escape if escape.startswith("/") else str(base / escape)
    with pytest.raises(EditorError):
        _editor(base).view(target)


# -- configurable working root ------------------------------------------------


def test_custom_root_resolves_all_four_commands_under_it(tmp_path):
    root = tmp_path / "srv" / "agent"
    root.mkdir(parents=True)
    ed = _editor(root)

    ed.create("notes.txt", "one\ntwo\n")
    created = root / "notes.txt"
    assert created.read_text(encoding="utf-8") == "one\ntwo\n"
    assert ed.view("notes.txt") == "     1\tone\n     2\ttwo\n"

    ed.str_replace("notes.txt", "one", "ONE")
    ed.insert("notes.txt", 2, "three")
    assert created.read_text(encoding="utf-8") == "ONE\ntwo\nthree\n"


def test_custom_root_binds_symlink_and_traversal_containment(tmp_path):
    root = tmp_path / "srv" / "agent"
    root.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")

    with pytest.raises(EditorError) as symlink_exc:
        _editor(root).view("link.txt")
    assert str(root.resolve()) in str(symlink_exc.value)

    for escape in ("../secret.txt", "/etc/hostname"):
        with pytest.raises(EditorError):
            _editor(root).view(escape)


def test_nonexistent_root_fails_loud_on_view_naming_root(tmp_path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(EditorError) as exc:
        _editor(missing).view("anything.txt")
    msg = str(exc.value)
    assert str(missing.resolve()) in msg
    assert "file not found" not in msg


def test_nonexistent_root_fails_loud_on_create_and_leaves_tree_uncreated(tmp_path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(EditorError) as exc:
        _editor(missing).create("new.txt", "hi\n")
    assert str(missing.resolve()) in str(exc.value)
    assert not missing.exists()  # regression lock: no silent mkdir -p of the root tree


def test_custom_root_escape_rejected_per_command(tmp_path):
    """AC: escape rejection is verified per command. ``_resolve`` is the shared
    chokepoint; this test locks that every command routes through it so a future
    refactor moving containment into command-specific paths would break here."""
    root = tmp_path / "srv" / "agent"
    root.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    ed = _editor(root)
    escape = "../secret.txt"

    with pytest.raises(EditorError):
        ed.view(escape)
    with pytest.raises(EditorError):
        ed.create(escape, "x\n")
    with pytest.raises(EditorError):
        ed.str_replace(escape, "a", "b")
    with pytest.raises(EditorError):
        ed.insert(escape, 1, "x")


# -- schema advertisement -----------------------------------------------------

_EXPECTED_PROPS = {
    "command",
    "path",
    "view_range",
    "file_text",
    "old_str",
    "new_str",
    "insert_line",
    "insert_text",
}


def test_schema_advertises_full_parameter_set_locally():
    schemas = _builtin_tool_schemas(["str_replace_editor"], {})
    assert "str_replace_editor" in schemas
    props = schemas["str_replace_editor"]["parameters"]["properties"]
    assert set(props) == _EXPECTED_PROPS
    assert props["command"]["enum"] == ["view", "create", "str_replace", "insert"]


def test_schema_advertised_when_compose_service_configured():
    """The schema provider tolerates the compose config kwargs, so the tool is
    not dropped from the LLM's view for a compose task."""
    schemas = _builtin_tool_schemas(
        ["str_replace_editor"],
        {"str_replace_editor": {"service": "app", "compose_project_prefix": "foo_"}},
    )
    assert "str_replace_editor" in schemas
    props = schemas["str_replace_editor"]["parameters"]["properties"]
    assert set(props) == _EXPECTED_PROPS
