"""The judge's ``search_kb`` tool: what it shows of a hit, and how much.

``judge_snippet_chars`` (``grading.llm_judge.customization``) is how much of each
hit's text the judge reads. The default, 200 characters with an ellipsis when cut,
is what every judge read before the field existed, so its output is pinned byte for
byte; ``None`` shows whole documents; a hit with a title gets a title line. The
customization model leaves the field off the wire at its default, so no
``TaskDescription`` moves, and reads ``null`` as a value.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_tools import SearchKbTool
from tolokaforge.core.grading.kb_search import DEFAULT_JUDGE_SNIPPET_CHARS, SearchHit
from tolokaforge.runner.models import JudgeCustomization, judge_snippet_chars_of

pytestmark = pytest.mark.unit

_LONG = "policy " * 60  # 420 characters


class _Hits:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        self.calls.append((query, top_k, alpha))
        return list(self.hits)


def _hit(text: str, *, title: str | None = None) -> SearchHit:
    return SearchHit(doc_id="doc-1", source="doc.md", score=1.23456, text=text, title=title)


def test_the_default_cut_is_two_hundred_characters_with_an_ellipsis() -> None:
    assert DEFAULT_JUDGE_SNIPPET_CHARS == 200
    result = SearchKbTool(_Hits([_hit(_LONG)])).execute(query="policy")
    assert result.success is True
    assert result.output == (
        "Found 1 relevant documents:\n"
        "\n"
        "\n[1] Document: doc-1"
        "\n    Source: doc.md"
        "\n    Score: 1.235"
        f"\n    Content: {_LONG[:200]}..."
    ), "the pre-field output, byte for byte"
    assert result.metadata == {"count": 1, "top_score": 1.23456}


def test_a_short_hit_is_not_cut_and_gets_no_ellipsis() -> None:
    result = SearchKbTool(_Hits([_hit("short")])).execute(query="policy")
    assert result.output.endswith("\n    Content: short")


def test_none_shows_the_whole_document() -> None:
    result = SearchKbTool(_Hits([_hit(_LONG)]), snippet_chars=None).execute(query="policy")
    assert result.output.endswith(f"\n    Content: {_LONG}")
    assert "..." not in result.output


def test_a_configured_length_cuts_there() -> None:
    result = SearchKbTool(_Hits([_hit(_LONG)]), snippet_chars=10).execute(query="policy")
    assert result.output.endswith(f"\n    Content: {_LONG[:10]}...")


def test_a_titled_hit_gets_a_title_line_between_document_and_source() -> None:
    hits = _Hits([_hit("text", title="Refund policy"), _hit("other")])
    result = SearchKbTool(hits).execute(query="policy")
    assert "\n[1] Document: doc-1\n    Title: Refund policy\n    Source: doc.md\n" in result.output
    assert "\n[2] Document: doc-1\n    Source: doc.md\n" in result.output, "no title, no line"


def test_the_tool_hands_the_judges_arguments_to_the_backend() -> None:
    hits = _Hits([_hit("text")])
    SearchKbTool(hits, snippet_chars=None).execute(query="refund", top_k=3, alpha=0.1)
    assert hits.calls == [("refund", 3, 0.1)]


class TestJudgeCustomizationSnippet:
    def test_the_default_is_two_hundred_and_left_off_the_dump(self) -> None:
        customization = JudgeCustomization()
        assert customization.judge_snippet_chars == 200
        assert "judge_snippet_chars" not in customization.model_dump()
        assert "judge_snippet_chars" not in customization.model_dump(mode="json")
        assert JudgeCustomization(disable_knowledge_search=True).model_dump() == {
            "disable_knowledge_search": True,
            "system_prompt": None,
            "include_agent_system_prompt": None,
        }, "a pre-field dump, byte for byte"

    def test_null_is_a_value_and_rides_the_wire(self) -> None:
        customization = JudgeCustomization.model_validate({"judge_snippet_chars": None})
        assert customization.judge_snippet_chars is None
        assert customization.model_dump()["judge_snippet_chars"] is None
        reloaded = JudgeCustomization.model_validate_json(customization.model_dump_json())
        assert reloaded.judge_snippet_chars is None

    def test_a_figure_rides_the_wire_and_round_trips(self) -> None:
        customization = JudgeCustomization(judge_snippet_chars=50)
        assert customization.model_dump()["judge_snippet_chars"] == 50
        assert JudgeCustomization.model_validate(customization.model_dump()) == customization

    @pytest.mark.parametrize("value", [0, -5, 2.5, "200", True])
    def test_anything_but_a_positive_integer_or_null_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError):
            JudgeCustomization.model_validate({"judge_snippet_chars": value})

    def test_the_resolver_reads_the_default_for_no_block(self) -> None:
        assert judge_snippet_chars_of(None) == 200
        assert judge_snippet_chars_of(JudgeCustomization()) == 200
        assert judge_snippet_chars_of(JudgeCustomization(judge_snippet_chars=None)) is None
        assert judge_snippet_chars_of(JudgeCustomization(judge_snippet_chars=7)) == 7
