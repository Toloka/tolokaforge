"""Unit tests for ``automation.classgate``.

The scenario mirrors the real deepseek-v4-flash-0731 PR #846, which registered
``OpenAISummaryReplayReasoningCodec`` with no unit test anywhere - the exact defect
``docs/ADD_NEW_MODEL.md`` step 5 forbids and nothing enforced.

The pure helpers are exercised on source strings; ``run`` is exercised end to end on
throwaway git repositories, because the defects it guards against live exactly in the
git seam (index vs worktree, CWD, error handling) that a monkeypatched shim cannot see.
"""

from __future__ import annotations

import subprocess

import automation.classgate as classgate
import pytest
from automation.classgate import Binding

pytestmark = pytest.mark.unit

BASE_PRESETS = """\
_REASONING_CODECS = {
    "openai": OpenAIReasoningCodec,
}

_RESPONSE_POLICIES = {
    "standard": StandardResponse,
}

_POLICY_REGISTRIES = {
    "reasoning_codec": _REASONING_CODECS,
    "response_policy": _RESPONSE_POLICIES,
}
"""


def _with_binding(line: str) -> str:
    """BASE_PRESETS with one extra line inside ``_REASONING_CODECS``."""
    return BASE_PRESETS.replace(
        '    "openai": OpenAIReasoningCodec,\n',
        f'    "openai": OpenAIReasoningCodec,\n    {line}\n',
    )


class TestRegistryBindings:
    def test_reads_the_baseline_bindings(self):
        assert classgate.registry_bindings(BASE_PRESETS) == {
            Binding("_REASONING_CODECS", "openai", "OpenAIReasoningCodec"),
            Binding("_RESPONSE_POLICIES", "standard", "StandardResponse"),
        }

    @pytest.mark.parametrize(
        ("line", "expected_value"),
        [
            # Every spelling the old diff-text regex silently missed - each one was a
            # fail-open evasion; the AST cannot be fooled by surface syntax.
            ("'new_codec': BrandNewCodec,", "BrandNewCodec"),  # single quotes
            ('"new_codec": BrandNewCodec', "BrandNewCodec"),  # no trailing comma
            ('"new_codec": codecs.BrandNewCodec,', "codecs.BrandNewCodec"),  # dotted
            ('"new_codec": BrandNewCodec(),', "BrandNewCodec()"),  # instance
            ('"new_codec": make_brand_new_codec,', "make_brand_new_codec"),  # factory
        ],
    )
    def test_binding_shape_variants_are_all_seen(self, line: str, expected_value: str):
        bindings = classgate.registry_bindings(_with_binding(line))
        assert Binding("_REASONING_CODECS", "new_codec", expected_value) in bindings

    def test_value_wrapped_onto_the_next_line_is_seen(self):
        source = BASE_PRESETS.replace(
            '    "openai": OpenAIReasoningCodec,\n',
            '    "openai": OpenAIReasoningCodec,\n    "new_codec":\n        BrandNewCodec,\n',
        )
        assert Binding("_REASONING_CODECS", "new_codec", "BrandNewCodec") in (
            classgate.registry_bindings(source)
        )

    def test_subscript_assignment_is_seen(self):
        source = BASE_PRESETS + '_REASONING_CODECS["late_codec"] = BrandNewCodec\n'
        assert Binding("_REASONING_CODECS", "late_codec", "BrandNewCodec") in (
            classgate.registry_bindings(source)
        )

    def test_update_call_is_seen(self):
        source = BASE_PRESETS + '_REASONING_CODECS.update({"late_codec": BrandNewCodec})\n'
        assert Binding("_REASONING_CODECS", "late_codec", "BrandNewCodec") in (
            classgate.registry_bindings(source)
        )

    def test_inline_slot_dict_in_the_index_is_seen(self):
        source = (
            'X = 1\n_POLICY_REGISTRIES = {\n    "reasoning_codec": {"inline": InlineCodec},\n}\n'
        )
        assert classgate.registry_bindings(source) == {
            Binding("reasoning_codec", "inline", "InlineCodec")
        }

    def test_non_slot_dicts_are_ignored(self):
        source = BASE_PRESETS + '_DEFAULT_PRESET_DATA = {"default": DefaultThing}\n'
        assert not any(
            binding.registry == "_DEFAULT_PRESET_DATA"
            for binding in classgate.registry_bindings(source)
        )

    @pytest.mark.parametrize(
        ("statement", "key"),
        [
            ('_REASONING_CODECS |= {"late_codec": BrandNewCodec}', "late_codec"),
            ("_REASONING_CODECS.update(late_codec=BrandNewCodec)", "late_codec"),
            ('_REASONING_CODECS.setdefault("late_codec", BrandNewCodec)', "late_codec"),
        ],
        ids=["augassign-or", "update-kwargs", "setdefault"],
    )
    def test_further_mutation_shapes_are_seen(self, statement: str, key: str):
        source = BASE_PRESETS + statement + "\n"
        assert Binding("_REASONING_CODECS", key, "BrandNewCodec") in (
            classgate.registry_bindings(source)
        )

    def test_registration_nested_in_a_conditional_is_seen(self):
        source = BASE_PRESETS + 'if True:\n    _REASONING_CODECS["late_codec"] = BrandNewCodec\n'
        assert Binding("_REASONING_CODECS", "late_codec", "BrandNewCodec") in (
            classgate.registry_bindings(source)
        )

    @pytest.mark.parametrize(
        "statement",
        [
            "_REASONING_CODECS = dict(late_codec=BrandNewCodec)",  # non-dict-literal reassign
            "_REASONING_CODECS += extra",  # AugAssign shape the gate cannot model
            "_REASONING_CODECS.update(**extra)",  # a splat hides its keys
            "_REASONING_CODECS.update(make_extra())",  # non-dict positional
            '_REASONING_CODECS.setdefault("late_codec")',  # no value bound
            '_REASONING_CODECS.pop("openai")',  # unrecognized mutation
        ],
        ids=["dict-call", "augassign-add", "splat", "call-arg", "setdefault-1", "pop"],
    )
    def test_unmodelable_mutations_fail_loud_not_open(self, statement: str):
        with pytest.raises(ValueError, match="unmodelable"):
            classgate.registry_bindings(BASE_PRESETS + statement + "\n")

    def test_read_only_uses_are_not_mutations(self):
        source = BASE_PRESETS + 'x = _REASONING_CODECS.get("openai")\nresolve(_REASONING_CODECS)\n'
        assert classgate.registry_bindings(source) == classgate.registry_bindings(BASE_PRESETS)

    def test_the_real_registry_file_parses_without_raising(self):
        # Pins the fail-loud net against false positives on the engine's actual
        # presets.py (its .get() resolution calls and argument-position passes must
        # stay recognized as reads).
        source = (classgate.REPO_ROOT / classgate.REGISTRY_FILE).read_text()
        bindings = classgate.registry_bindings(source)
        assert len(bindings) >= 20
        assert len({binding.registry for binding in bindings}) == 6

    def test_missing_registry_index_fails_loud(self):
        with pytest.raises(ValueError, match="no _POLICY_REGISTRIES slots"):
            classgate.registry_bindings("_REASONING_CODECS = {'a': A}\n")

    def test_unparsable_source_raises(self):
        with pytest.raises(SyntaxError):
            classgate.registry_bindings("this is not python {")


class TestAddedBindings:
    def test_a_new_binding_is_reported(self):
        staged = _with_binding('"new_codec": BrandNewCodec,')
        assert classgate.added_bindings(staged, BASE_PRESETS) == [
            Binding("_REASONING_CODECS", "new_codec", "BrandNewCodec")
        ]

    def test_moved_and_reordered_lines_are_not_new(self):
        """Alphabetizing a dict while inserting nothing must not hard-fail the gate on
        pre-existing classes (set semantics, not line semantics)."""
        reordered = BASE_PRESETS.replace(
            '_POLICY_REGISTRIES = {\n    "reasoning_codec": _REASONING_CODECS,\n'
            '    "response_policy": _RESPONSE_POLICIES,\n}\n',
            '_POLICY_REGISTRIES = {\n    "response_policy": _RESPONSE_POLICIES,\n'
            '    "reasoning_codec": _REASONING_CODECS,\n}\n',
        )
        assert classgate.added_bindings(reordered, BASE_PRESETS) == []

    def test_rebinding_an_existing_key_is_reported(self):
        staged = BASE_PRESETS.replace("OpenAIReasoningCodec", "ReplacementCodec")
        assert classgate.added_bindings(staged, BASE_PRESETS) == [
            Binding("_REASONING_CODECS", "openai", "ReplacementCodec")
        ]


class TestBoundName:
    @pytest.mark.parametrize(
        ("value", "name"),
        [
            ("BrandNewCodec", "BrandNewCodec"),
            ("codecs.BrandNewCodec", "BrandNewCodec"),
            ("BrandNewCodec()", "BrandNewCodec"),
            ("make_brand_new_codec", "make_brand_new_codec"),
        ],
    )
    def test_names_the_identifier_a_test_must_mention(self, value: str, name: str):
        assert classgate.bound_name(value) == name


class TestUntested:
    def test_flags_a_class_no_test_mentions(self):
        assert classgate.untested(["BrandNewCodec"], "def test_something(): pass") == [
            "BrandNewCodec"
        ]

    def test_accepts_a_class_a_test_mentions(self):
        blob = "from x import BrandNewCodec\ndef test_it(): ..."
        assert classgate.untested(["BrandNewCodec"], blob) == []

    def test_substring_of_an_existing_mention_does_not_vouch(self):
        """`MinimaxM3TagRecoveryResponse` in an existing test must not satisfy a NEW
        `TagRecoveryResponse` - word-boundary, not substring."""
        blob = "from y import MinimaxM3TagRecoveryResponse\ndef test_m3(): ..."
        assert classgate.untested(["TagRecoveryResponse"], blob) == ["TagRecoveryResponse"]


class TestUnreferenced:
    BINDINGS = [Binding("_REASONING_CODECS", "openai_summary_replay", "BrandNewCodec")]

    def test_flags_a_class_nothing_references(self):
        overlay = "presets:\n  x:\n    match: ['a']\n"
        assert classgate.unreferenced(self.BINDINGS, overlay, "") == ["BrandNewCodec"]

    def test_registry_key_in_a_value_position_counts(self):
        overlay = "presets:\n  x:\n    reasoning_codec: openai_summary_replay\n"
        assert classgate.unreferenced(self.BINDINGS, overlay, "") == []

    def test_quoted_value_position_counts(self):
        overlay = 'presets:\n  x:\n    reasoning_codec: "openai_summary_replay"\n'
        assert classgate.unreferenced(self.BINDINGS, overlay, "") == []

    def test_key_inside_a_match_glob_does_not_count(self):
        """A short family key must not be auto-referenced by a slug that merely
        contains it - `qwen` in `match: ["qwen/qwen3.8-max"]` is not a reference."""
        bindings = [Binding("_RESPONSE_POLICIES", "qwen", "QwenRecovery")]
        overlay = 'presets:\n  x:\n    match: ["qwen/qwen3.8-max"]\n'
        assert classgate.unreferenced(bindings, overlay, "") == ["QwenRecovery"]

    def test_class_name_mention_counts(self):
        overlay = "# uses BrandNewCodec\n"
        assert classgate.unreferenced(self.BINDINGS, overlay, "") == []

    def test_staged_preset_file_reference_counts(self):
        presets = "presets:\n  folded:\n    reasoning_codec: openai_summary_replay\n"
        assert classgate.unreferenced(self.BINDINGS, "", presets) == []

    def test_any_of_several_keys_counts(self):
        bindings = self.BINDINGS + [Binding("_REASONING_CODECS", "second_alias", "BrandNewCodec")]
        overlay = "presets:\n  x:\n    reasoning_codec: second_alias\n"
        assert classgate.unreferenced(bindings, overlay, "") == []


# --- run(): end to end on throwaway git repos -----------------------------------


def _git(repo, *args) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


NEW_BINDING_PRESETS = BASE_PRESETS.replace(
    '    "openai": OpenAIReasoningCodec,\n',
    '    "openai": OpenAIReasoningCodec,\n    "openai_summary_replay": BrandNewCodec,\n',
)


@pytest.fixture()
def repo(tmp_path):
    """A committed baseline repo shaped like the paths the gate reads."""
    root = tmp_path / "repo"
    (root / "tolokaforge/core/llm").mkdir(parents=True)
    (root / "tolokaforge/core/data").mkdir(parents=True)
    (root / "tests/unit/llm").mkdir(parents=True)
    (root / "tolokaforge/core/llm/presets.py").write_text(BASE_PRESETS)
    (root / "tolokaforge/core/data/model_presets.yaml").write_text("presets: {}\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _stage_new_binding(root) -> None:
    (root / "tolokaforge/core/llm/presets.py").write_text(NEW_BINDING_PRESETS)
    _git(root, "add", "tolokaforge/core/llm/presets.py")


def _overlay(tmp_path, text="presets:\n  x:\n    reasoning_codec: openai_summary_replay\n"):
    path = tmp_path / "overlay.yaml"
    path.write_text(text)
    return str(path)


class TestRun:
    def test_no_new_binding_passes(self, repo):
        assert classgate.run(root=str(repo)) == 0

    def test_staged_test_and_overlay_reference_pass(self, repo, tmp_path):
        _stage_new_binding(repo)
        (repo / "tests/unit/llm/test_codec.py").write_text("BrandNewCodec\n")
        _git(repo, "add", "tests/unit/llm/test_codec.py")
        assert classgate.run(overlay_path=_overlay(tmp_path), root=str(repo)) == 0

    def test_worktree_only_test_does_not_count(self, repo, tmp_path, capsys):
        """The finalize commit ships the INDEX. A test that exists only in the worktree
        would satisfy a worktree-reading gate and then vanish from the commit - the
        false assurance the gate exists to prevent - so it must NOT count."""
        _stage_new_binding(repo)
        (repo / "tests/unit/llm/test_codec.py").write_text("BrandNewCodec\n")  # never added
        assert classgate.run(overlay_path=_overlay(tmp_path), root=str(repo)) == 1
        assert "UNTESTED-CLASS" in capsys.readouterr().out

    def test_unreferenced_class_fails_without_any_reference(self, repo, tmp_path, capsys):
        _stage_new_binding(repo)
        (repo / "tests/unit/llm/test_codec.py").write_text("BrandNewCodec\n")
        _git(repo, "add", "tests/unit/llm/test_codec.py")
        assert classgate.run(overlay_path=None, root=str(repo)) == 1
        assert "UNREFERENCED-CLASS" in capsys.readouterr().out

    def test_staged_model_presets_reference_passes_without_an_overlay(self, repo):
        _stage_new_binding(repo)
        (repo / "tests/unit/llm/test_codec.py").write_text("BrandNewCodec\n")
        _git(repo, "add", "tests/unit/llm/test_codec.py")
        (repo / "tolokaforge/core/data/model_presets.yaml").write_text(
            "presets:\n  folded:\n    reasoning_codec: openai_summary_replay\n"
        )
        _git(repo, "add", "tolokaforge/core/data/model_presets.yaml")
        assert classgate.run(overlay_path=None, root=str(repo)) == 0

    def test_cwd_does_not_matter(self, repo, tmp_path, monkeypatch):
        """The old gate read git from the caller's CWD: from a subdirectory every git
        answer silently emptied and the gate no-opped. The root is now anchored."""
        _stage_new_binding(repo)
        (repo / "tests/unit/llm/test_codec.py").write_text("BrandNewCodec\n")
        _git(repo, "add", "tests/unit/llm/test_codec.py")
        monkeypatch.chdir(repo / "tests")
        assert classgate.run(overlay_path=_overlay(tmp_path), root=str(repo)) == 0
        monkeypatch.chdir(tmp_path)  # unrelated dir
        assert classgate.run(overlay_path=_overlay(tmp_path), root=str(repo)) == 0

    def test_non_git_root_fails_loud_not_open(self, tmp_path, capsys):
        """git exiting 128 used to read as "no new policy class registered" (rc=0).
        A guard must fail loud, never skip."""
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert classgate.run(root=str(outside)) == 1
        assert "could not read the staged tree" in capsys.readouterr().out

    def test_reordered_registry_does_not_false_positive(self, repo, tmp_path):
        """A move is a -/+ pair in diff text; the set diff must not report it."""
        reordered = BASE_PRESETS.replace(
            '_REASONING_CODECS = {\n    "openai": OpenAIReasoningCodec,\n}\n',
            '_REASONING_CODECS = {\n\n    "openai": OpenAIReasoningCodec,\n}\n',
        )
        (repo / "tolokaforge/core/llm/presets.py").write_text(reordered)
        _git(repo, "add", "tolokaforge/core/llm/presets.py")
        assert classgate.run(root=str(repo)) == 0
