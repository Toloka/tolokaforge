"""Unit test for the harness-registry data-vs-code classifier.

Locks the path taxonomy (data → Bucket A, code → Bucket B) independently
of any git state — same discipline as
``tests/unit/test_models_wheel_replay_classifier.py``."""

from __future__ import annotations

import pytest
from automation.harness_bucket_classifier import (
    BUCKET_A_ALLOWED_FILES,
    BUCKET_A_ALLOWED_PREFIXES,
    HarnessBucket,
    classify_harness_paths,
)


class TestClassifyHarnessPaths:
    """The classifier is pure over the touched-file set."""

    def test_empty_input_is_bucket_a_with_empty_adapter_paths(self) -> None:
        result = classify_harness_paths([])
        assert result.bucket is HarnessBucket.A
        assert result.adapter_paths == ()
        assert "empty" in result.reason

    def test_only_data_yaml_is_bucket_a(self) -> None:
        result = classify_harness_paths(
            [
                "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml",
            ]
        )
        assert result.bucket is HarnessBucket.A
        assert result.adapter_paths == ()

    def test_data_yaml_at_its_pre_adr_0035_path_is_still_bucket_a(self) -> None:
        """Commits predating the package split keep the classification they
        earned; the metric replays history, so reclassifying it would rewrite
        what the historical distribution says."""
        result = classify_harness_paths(
            [
                "external_adapters/tolokaforge-adapter-terminal-bench/src/"
                "tolokaforge_adapter_terminal_bench/data/harnesses.yaml",
            ]
        )
        assert result.bucket is HarnessBucket.A
        assert result.adapter_paths == ()

    def test_only_snapshot_files_are_bucket_a(self) -> None:
        result = classify_harness_paths(
            [
                "tests/canonical/snapshots/tbench_echo_hello_harness/harness_spec.json",
                "tests/canonical/snapshots/tbench_echo_hello_harness/synthesised_compose.json",
            ]
        )
        assert result.bucket is HarnessBucket.A

    def test_only_examples_are_bucket_a(self) -> None:
        result = classify_harness_paths(
            [
                "examples/terminal_bench/gemini_litellm_overlay.yaml",
                "examples/terminal_bench/run_harness.yaml",
            ]
        )
        assert result.bucket is HarnessBucket.A

    def test_adapter_readme_is_bucket_a(self) -> None:
        result = classify_harness_paths(
            ["external_adapters/tolokaforge-adapter-terminal-bench/README.md"]
        )
        assert result.bucket is HarnessBucket.A

    def test_adr_files_are_bucket_a(self) -> None:
        result = classify_harness_paths(
            [
                "docs/adr/0033-external-harness-registry.md",
                "docs/adr/0034-external-harness-plugin-discovery.md",
                "docs/adr/0036-tolokaforge-coding-harnesses-split.md",
                "docs/adr/0037-runtime-gateway-as-harness-data.md",
            ]
        )
        assert result.bucket is HarnessBucket.A

    def test_a_harness_adr_alone_is_bucket_a(self) -> None:
        """An ADR-only commit documents the surface rather than implementing
        it. Classified B, it would count against the data-vs-code metric the
        primitive exists to measure."""
        result = classify_harness_paths(["docs/adr/0037-runtime-gateway-as-harness-data.md"])
        assert result.bucket is HarnessBucket.A
        assert result.adapter_paths == ()

    @pytest.mark.parametrize(
        "path",
        [
            # Python module — where argv assembly + spec model live.
            "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/_registry.py",
            # Shell installer — the four-way install dispatcher.
            "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/install-harness.sh",
            # The same two files at their pre-ADR-0036 paths.
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/harness/__init__.py",
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/harness/install-harness.sh",
            # Compose synthesis — skill-delivery mechanism lives here.
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/compose_synthesis.py",
            # Test code that exercises adapter Python.
            "tests/unit/test_terminal_bench.py",
            "tests/canonical/test_terminal_bench_adapter_canon.py",
        ],
    )
    def test_python_or_shell_is_bucket_b(self, path: str) -> None:
        result = classify_harness_paths([path])
        assert result.bucket is HarnessBucket.B
        assert result.adapter_paths == (path,)

    def test_mixed_touch_is_bucket_b_and_lists_only_offending_paths(self) -> None:
        yaml_path = (
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/data/harnesses.yaml"
        )
        py_path = (
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/harness/__init__.py"
        )
        result = classify_harness_paths([yaml_path, py_path])
        assert result.bucket is HarnessBucket.B
        assert result.adapter_paths == (py_path,)

    def test_paths_are_deduped(self) -> None:
        result = classify_harness_paths(
            [
                "tests/canonical/snapshots/tbench_echo_hello_harness/harness_spec.json",
                "tests/canonical/snapshots/tbench_echo_hello_harness/harness_spec.json",
            ]
        )
        assert result.bucket is HarnessBucket.A

    def test_adapter_paths_are_sorted(self) -> None:
        py_a = (
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/harness/__init__.py"
        )
        py_b = (
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/compose_synthesis.py"
        )
        result = classify_harness_paths([py_a, py_b])
        # Sorted order groups compose_synthesis.py before harness/__init__.py
        # alphabetically after the shared prefix.
        assert result.adapter_paths == tuple(sorted([py_a, py_b]))


class TestAllowListShapeInvariants:
    """The allow-lists themselves are stable data; if either changes, the
    replay snapshot needs a deliberate regen."""

    def test_files_allow_list_contains_the_documented_files(self) -> None:
        assert (
            frozenset(
                {
                    "external_adapters/tolokaforge-adapter-terminal-bench/README.md",
                    "docs/adr/0033-external-harness-registry.md",
                    "docs/adr/0034-external-harness-plugin-discovery.md",
                    "docs/adr/0036-tolokaforge-coding-harnesses-split.md",
                    "docs/adr/0037-runtime-gateway-as-harness-data.md",
                }
            )
            == BUCKET_A_ALLOWED_FILES
        )

    def test_prefix_allow_list_contains_documented_prefixes(self) -> None:
        assert set(BUCKET_A_ALLOWED_PREFIXES) == {
            "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/",
            "external_adapters/tolokaforge-adapter-terminal-bench/src/"
            "tolokaforge_adapter_terminal_bench/data/",
            "tests/canonical/snapshots/tbench_echo_hello_harness/",
            "tests/canonical/snapshots/tbench_echo_hello_skills_harness/",
            "examples/terminal_bench/",
        }
