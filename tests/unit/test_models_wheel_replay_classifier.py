"""Unit tests for the models-wheel replay Bucket A/B classifier.

Locks the classifier's decisions on every historical ``integrate: ...``
touched-path set reachable from HEAD, plus synthetic edge cases
(empty diff, mixed A/B, unknown-prefix paths, both cert-registry paths,
sorted-and-deduped output) and dataclass/enum invariants.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.canonical._models_wheel_replay.classifier import (
    Bucket,
    Classification,
    classify_paths,
)

pytestmark = pytest.mark.unit


# Each row is: label, touched, expected_bucket, expected_engine_paths.
# ``touched`` is the ``git show --name-only`` output for the auto-integration
# commit identified in the label; ``expected_engine_paths`` is the sorted,
# deduped subset that falls outside the Bucket-A allow-list.
_HISTORICAL_CASES: tuple[tuple[str, tuple[str, ...], Bucket, tuple[str, ...]], ...] = (
    (
        "muse-spark-1.1 #474 (3c26098c)",
        (
            "tests/integration/llm/registry.py",
            "tests/integration/llm/test_implicit_prompt_caching_unsupported_ratchet.py",
            "tolokaforge/core/data/pricing.json",
        ),
        Bucket.B,
        ("tests/integration/llm/test_implicit_prompt_caching_unsupported_ratchet.py",),
    ),
    (
        "kimi-k3 #475 (f06d1c44)",
        (
            "tests/integration/llm/registry.py",
            "tolokaforge/core/data/pricing.json",
        ),
        Bucket.A,
        (),
    ),
    (
        "gemini-3.5-flash #560 (e074af81)",
        (
            "tests/integration/llm/registry.py",
            "tolokaforge/core/data/model_presets.yaml",
            "tolokaforge/core/data/pricing.json",
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/response_policy.py",
            "tolokaforge/core/llm/schema_sanitizer.py",
        ),
        Bucket.B,
        (
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/response_policy.py",
            "tolokaforge/core/llm/schema_sanitizer.py",
        ),
    ),
    (
        "claude-opus-5 #614 (0fd18013)",
        (
            "tests/integration/llm/registry.py",
            "tolokaforge/core/data/model_presets.yaml",
            "tolokaforge/core/data/pricing.json",
        ),
        Bucket.A,
        (),
    ),
    (
        "inkling #719 (482c8195)",
        (
            "tests/integration/llm/registry.py",
            "tolokaforge/core/data/model_presets.yaml",
            "tolokaforge/core/data/pricing.json",
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/prompt_policy.py",
        ),
        Bucket.B,
        (
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/prompt_policy.py",
        ),
    ),
    (
        "qwen3.8-max #845 (fb4daef3)",
        (
            "tests/integration/llm/registry.py",
            "tolokaforge/core/data/model_presets.yaml",
            "tolokaforge/core/data/pricing.json",
        ),
        Bucket.A,
        (),
    ),
    (
        "deepseek-v4-flash-0731 #846 (c09824e1)",
        (
            "tests/integration/llm/registry.py",
            "tests/unit/llm/fixtures/openrouter_deepseek_v4_reasoning_response.json",
            "tests/unit/llm/test_reasoning_codec_openai_summary_replay.py",
            "tolokaforge/core/data/model_presets.yaml",
            "tolokaforge/core/data/pricing.json",
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/reasoning_codec.py",
        ),
        Bucket.B,
        (
            "tests/unit/llm/fixtures/openrouter_deepseek_v4_reasoning_response.json",
            "tests/unit/llm/test_reasoning_codec_openai_summary_replay.py",
            "tolokaforge/core/llm/__init__.py",
            "tolokaforge/core/llm/presets.py",
            "tolokaforge/core/llm/reasoning_codec.py",
        ),
    ),
)


@pytest.mark.parametrize(
    "touched,expected_bucket,expected_engine_paths",
    [(touched, bucket, engine) for _, touched, bucket, engine in _HISTORICAL_CASES],
    ids=[case[0] for case in _HISTORICAL_CASES],
)
def test_historical_touched_paths_classify(
    touched: tuple[str, ...],
    expected_bucket: Bucket,
    expected_engine_paths: tuple[str, ...],
) -> None:
    result = classify_paths(touched)
    assert result.bucket == expected_bucket
    assert result.engine_paths == expected_engine_paths


def test_empty_diff_is_bucket_a() -> None:
    result = classify_paths(())
    assert result.bucket == Bucket.A
    assert result.engine_paths == ()
    assert result.reason == "empty diff"


def test_only_pre_move_cert_registry_is_bucket_a() -> None:
    result = classify_paths(("tests/integration/llm/registry.py",))
    assert result.bucket == Bucket.A
    assert result.engine_paths == ()


def test_only_post_move_cert_registry_is_bucket_a() -> None:
    result = classify_paths(("tolokaforge/testing/certify/_registry.py",))
    assert result.bucket == Bucket.A
    assert result.engine_paths == ()


def test_only_pricing_is_bucket_a() -> None:
    result = classify_paths(("tolokaforge/core/data/pricing.json",))
    assert result.bucket == Bucket.A
    assert result.engine_paths == ()


def test_future_models_wheel_prefix_is_bucket_a() -> None:
    result = classify_paths(("tolokaforge_models/policies/gemini.py",))
    assert result.bucket == Bucket.A
    assert result.engine_paths == ()


def test_pricing_plus_engine_path_is_bucket_b() -> None:
    result = classify_paths(
        (
            "tolokaforge/core/data/pricing.json",
            "tolokaforge/core/llm/client.py",
        )
    )
    assert result.bucket == Bucket.B
    assert result.engine_paths == ("tolokaforge/core/llm/client.py",)


def test_two_engine_paths_are_sorted() -> None:
    result = classify_paths(
        (
            "tolokaforge/core/llm/z_late.py",
            "tolokaforge/core/llm/a_early.py",
            "tolokaforge/core/data/pricing.json",
        )
    )
    assert result.bucket == Bucket.B
    assert result.engine_paths == (
        "tolokaforge/core/llm/a_early.py",
        "tolokaforge/core/llm/z_late.py",
    )


@pytest.mark.parametrize("outsider", ["README.md", ".github/workflows/ci.yml"])
def test_path_outside_allow_list_is_bucket_b(outsider: str) -> None:
    result = classify_paths((outsider,))
    assert result.bucket == Bucket.B
    assert result.engine_paths == (outsider,)


def test_duplicate_touches_are_deduped() -> None:
    result = classify_paths(
        (
            "tolokaforge/core/llm/client.py",
            "tolokaforge/core/llm/client.py",
        )
    )
    assert result.engine_paths == ("tolokaforge/core/llm/client.py",)


def test_identical_inputs_produce_identical_classification() -> None:
    inputs = (
        "tolokaforge/core/data/pricing.json",
        "tolokaforge/core/llm/client.py",
    )
    assert classify_paths(inputs) == classify_paths(inputs)


def test_input_order_does_not_affect_classification() -> None:
    forwards = classify_paths(
        (
            "tolokaforge/core/data/pricing.json",
            "tolokaforge/core/llm/z_late.py",
            "tolokaforge/core/llm/a_early.py",
        )
    )
    backwards = classify_paths(
        (
            "tolokaforge/core/llm/a_early.py",
            "tolokaforge/core/llm/z_late.py",
            "tolokaforge/core/data/pricing.json",
        )
    )
    assert forwards == backwards


def test_classification_is_frozen() -> None:
    result = classify_paths(())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.bucket = Bucket.B  # type: ignore[misc]


def test_classification_field_types() -> None:
    result = classify_paths(("tolokaforge/core/data/pricing.json",))
    assert isinstance(result, Classification)
    assert isinstance(result.bucket, Bucket)
    assert isinstance(result.reason, str)
    assert isinstance(result.engine_paths, tuple)


def test_bucket_is_str_enum_round_trip() -> None:
    assert Bucket("A") is Bucket.A
    assert Bucket("B") is Bucket.B
    assert Bucket.A.value == "A"
    assert Bucket.B.value == "B"
    assert isinstance(Bucket.A, str)
