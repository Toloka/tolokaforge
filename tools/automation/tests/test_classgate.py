"""Unit tests for ``automation.classgate``.

The scenario mirrors the real deepseek-v4-flash-0731 PR #846, which registered
``OpenAISummaryReplayReasoningCodec`` with no unit test anywhere - the exact defect
``docs/ADD_NEW_MODEL.md`` step 5 forbids and nothing enforced.
"""

from __future__ import annotations

import automation.classgate as classgate
import pytest

pytestmark = pytest.mark.unit

# Trimmed to the shape ``git diff --cached --unified=0`` emits for the registry file.
PR846_DIFF = """\
diff --git a/tolokaforge/core/llm/presets.py b/tolokaforge/core/llm/presets.py
--- a/tolokaforge/core/llm/presets.py
+++ b/tolokaforge/core/llm/presets.py
@@ -112,0 +113 @@
+    "openai_summary_replay": OpenAISummaryReplayReasoningCodec,
"""

OTHER_FILE_DIFF = """\
diff --git a/tolokaforge/core/llm/response_policy.py b/tolokaforge/core/llm/response_policy.py
--- a/tolokaforge/core/llm/response_policy.py
+++ b/tolokaforge/core/llm/response_policy.py
@@ -10,0 +11 @@
+class SomeHelper:
+    "not a registry binding"
"""


class TestAddedRegistryClasses:
    def test_finds_the_new_codec_binding(self):
        assert classgate.added_registry_classes(PR846_DIFF) == ["OpenAISummaryReplayReasoningCodec"]

    def test_ignores_added_lines_in_other_files(self):
        """A class DEFINED elsewhere is not a registration - only presets.py counts."""
        assert classgate.added_registry_classes(OTHER_FILE_DIFF) == []

    def test_ignores_removed_and_context_lines(self):
        diff = PR846_DIFF.replace(
            '+    "openai_summary_replay": OpenAISummaryReplayReasoningCodec,',
            '-    "openai_summary_replay": OpenAISummaryReplayReasoningCodec,\n'
            '     "openai": OpenAIReasoningCodec,',
        )
        assert classgate.added_registry_classes(diff) == []

    def test_deduplicates_repeated_bindings(self):
        diff = PR846_DIFF + '+    "alias": OpenAISummaryReplayReasoningCodec,\n'
        assert classgate.added_registry_classes(diff) == ["OpenAISummaryReplayReasoningCodec"]

    def test_empty_diff_is_empty(self):
        assert classgate.added_registry_classes("") == []


class TestUntested:
    def test_flags_a_class_no_test_mentions(self):
        assert classgate.untested(
            ["OpenAISummaryReplayReasoningCodec"], "def test_something(): pass"
        ) == ["OpenAISummaryReplayReasoningCodec"]

    def test_accepts_a_class_a_test_mentions(self):
        blob = "from x import OpenAISummaryReplayReasoningCodec\ndef test_it(): ..."
        assert classgate.untested(["OpenAISummaryReplayReasoningCodec"], blob) == []


class TestUnreferenced:
    def test_flags_a_class_the_overlay_never_uses(self):
        assert classgate.unreferenced(
            ["OpenAISummaryReplayReasoningCodec"], "presets:\n  x:\n    match: ['a']\n", PR846_DIFF
        ) == ["OpenAISummaryReplayReasoningCodec"]

    def test_registry_key_in_the_overlay_counts_as_a_reference(self):
        """Overlays reference a class by its registry KEY, not its class name."""
        overlay = "presets:\n  x:\n    reasoning_codec: openai_summary_replay\n"
        assert (
            classgate.unreferenced(["OpenAISummaryReplayReasoningCodec"], overlay, PR846_DIFF) == []
        )

    def test_class_name_in_the_overlay_also_counts(self):
        overlay = "# uses OpenAISummaryReplayReasoningCodec\n"
        assert (
            classgate.unreferenced(["OpenAISummaryReplayReasoningCodec"], overlay, PR846_DIFF) == []
        )

    def test_any_of_several_keys_counts_as_a_reference(self):
        """A class bound under two keys is referenced if the overlay uses EITHER one."""
        diff = PR846_DIFF + '+    "second_alias": OpenAISummaryReplayReasoningCodec,\n'
        for key in ("openai_summary_replay", "second_alias"):
            overlay = f"presets:\n  x:\n    reasoning_codec: {key}\n"
            assert (
                classgate.unreferenced(["OpenAISummaryReplayReasoningCodec"], overlay, diff) == []
            ), key


class TestAddedBindings:
    def test_returns_key_class_pairs(self):
        assert classgate.added_bindings(PR846_DIFF) == [
            ("openai_summary_replay", "OpenAISummaryReplayReasoningCodec")
        ]

    def test_keeps_every_key_for_a_multiply_bound_class(self):
        diff = PR846_DIFF + '+    "second_alias": OpenAISummaryReplayReasoningCodec,\n'
        assert classgate.added_bindings(diff) == [
            ("openai_summary_replay", "OpenAISummaryReplayReasoningCodec"),
            ("second_alias", "OpenAISummaryReplayReasoningCodec"),
        ]

    def test_ignores_underscore_prefixed_registry_slot_lines(self):
        """``_POLICY_REGISTRIES`` slot values are underscore-prefixed, not classes."""
        diff = (
            "+++ b/tolokaforge/core/llm/presets.py\n" '+    "reasoning_codec": _REASONING_CODECS,\n'
        )
        assert classgate.added_bindings(diff) == []


class TestReadTests:
    def test_missing_dir_is_empty_not_an_error(self):
        assert classgate.read_tests("does/not/exist") == ""

    def test_concatenates_test_sources(self, tmp_path):
        (tmp_path / "test_a.py").write_text("MarkerClassA")
        (tmp_path / "helper.py").write_text("MarkerNotATest")
        blob = classgate.read_tests(str(tmp_path))
        assert "MarkerClassA" in blob
        assert "MarkerNotATest" not in blob


class TestRun:
    def test_no_new_class_passes(self, monkeypatch):
        monkeypatch.setattr(classgate, "_staged_diff", lambda: OTHER_FILE_DIFF)
        assert classgate.run() == 0

    def test_pr846_shape_fails_on_the_missing_unit_test(self, monkeypatch, tmp_path):
        monkeypatch.setattr(classgate, "_staged_diff", lambda: PR846_DIFF)
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text("presets:\n  x:\n    reasoning_codec: openai_summary_replay\n")
        empty_tests = tmp_path / "tests"
        empty_tests.mkdir()
        assert classgate.run(overlay_path=str(overlay), tests_dir=str(empty_tests)) == 1

    def test_passes_once_a_unit_test_covers_the_class(self, monkeypatch, tmp_path):
        monkeypatch.setattr(classgate, "_staged_diff", lambda: PR846_DIFF)
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text("presets:\n  x:\n    reasoning_codec: openai_summary_replay\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_codec.py").write_text(
            "from y import OpenAISummaryReplayReasoningCodec\ndef test_c(): ..."
        )
        assert classgate.run(overlay_path=str(overlay), tests_dir=str(tests)) == 0

    def test_missing_overlay_still_flags_the_unreferenced_class(self, monkeypatch, tmp_path):
        monkeypatch.setattr(classgate, "_staged_diff", lambda: PR846_DIFF)
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_codec.py").write_text("OpenAISummaryReplayReasoningCodec")
        assert classgate.run(overlay_path=None, tests_dir=str(tests)) == 1
