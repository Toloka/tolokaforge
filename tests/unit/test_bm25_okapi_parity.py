"""``OkapiBm25`` scores are bit-identical to ``rank_bm25`` 0.2.2's ``BM25Okapi``.

Two checks, both comparing floats with ``==`` — bit identity, never a tolerance,
because a last-bit difference reorders ties and a ``bm25`` task's ranking is what
its grading reads:

* against the committed fixture ``tests/fixtures/bm25_okapi_reference.json``,
  generated once with the reference library by
  ``scripts/tests/generate_bm25_okapi_reference.py`` — this runs everywhere;
* against ``rank_bm25`` itself, on the fixture's corpora and on fresh random ones,
  where the library is installed (the dev environment).
"""

from __future__ import annotations

import importlib.metadata
import json
import random
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.search.bm25 import OkapiBm25

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "bm25_okapi_reference.json"
_REFERENCE_VERSION = "0.2.2"


def _cases() -> list[dict[str, Any]]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["rank_bm25_version"] == _REFERENCE_VERSION
    return data["cases"]


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["name"] for case in cases]


_CASES = _cases()


@pytest.mark.parametrize("case", _CASES, ids=_ids(_CASES))
def test_scores_match_the_committed_reference_bit_for_bit(case: dict[str, Any]) -> None:
    scorer = OkapiBm25(case["corpus"], k1=case["k1"], b=case["b"], epsilon=case["epsilon"])
    for query, expected in zip(case["queries"], case["expected"], strict=True):
        scores = scorer.get_scores(query)
        assert all(isinstance(score, float) for score in scores)
        assert scores == expected, f"query {query!r} scored differently from rank_bm25"


def test_the_fixture_covers_what_the_port_must_get_right() -> None:
    """The cases exercise the subtleties, not only the happy path."""
    corpora = [case["corpus"] for case in _CASES]
    assert any(len(corpus) >= 250 for corpus in corpora), "hundreds of documents"
    assert any(not query for case in _CASES for query in case["queries"]), "an empty query"
    assert any(
        len(query) != len(set(query)) for case in _CASES for query in case["queries"]
    ), "a repeated query token"
    assert any(
        any(ord(ch) > 127 for token in document for ch in token)
        for corpus in corpora
        for document in corpus
    ), "non-ASCII tokens"
    negative = [
        case for case in _CASES if any(idf < 0 for idf in _raw_idfs(case["corpus"]).values())
    ]
    assert negative, "a term in more than half the documents (negative IDF)"
    assert {(case["k1"], case["b"], case["epsilon"]) for case in _CASES} != {(1.5, 0.75, 0.25)}


def _raw_idfs(corpus: list[list[str]]) -> dict[str, float]:
    import math

    document_frequency: dict[str, int] = {}
    for document in corpus:
        for token in set(document):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    size = len(corpus)
    return {
        token: math.log(size - df + 0.5) - math.log(df + 0.5)
        for token, df in document_frequency.items()
    }


class TestAgainstTheInstalledLibrary:
    @pytest.fixture(autouse=True)
    def rank_bm25(self) -> Any:
        library = pytest.importorskip("rank_bm25")
        installed = importlib.metadata.version("rank_bm25")
        if installed != _REFERENCE_VERSION:
            pytest.skip(
                f"rank_bm25 {installed} installed; the port reproduces {_REFERENCE_VERSION}"
            )
        return library

    @pytest.mark.parametrize("case", _CASES, ids=_ids(_CASES))
    def test_the_fixture_is_what_the_library_gives(self, rank_bm25: Any, case: dict) -> None:
        reference = rank_bm25.BM25Okapi(
            case["corpus"], k1=case["k1"], b=case["b"], epsilon=case["epsilon"]
        )
        for query, expected in zip(case["queries"], case["expected"], strict=True):
            assert reference.get_scores(query).tolist() == expected

    @pytest.mark.parametrize("seed", range(5))
    def test_fresh_random_corpora_score_identically(self, rank_bm25: Any, seed: int) -> None:
        rng = random.Random(seed)
        vocabulary = [f"w{i}" for i in range(rng.randint(5, 70))] + ["ü", "日本", "the"]
        corpus = [
            [rng.choice(vocabulary) for _ in range(rng.randint(1, 40))]
            for _ in range(rng.randint(50, 400))
        ]
        k1, b, epsilon = rng.choice([(1.5, 0.75, 0.25), (1.2, 0.5, 0.1), (2.0, 1.0, 0.5)])
        reference = rank_bm25.BM25Okapi(corpus, k1=k1, b=b, epsilon=epsilon)
        port = OkapiBm25(corpus, k1=k1, b=b, epsilon=epsilon)
        assert port.idf == reference.idf
        assert port.average_idf == reference.average_idf
        for _ in range(15):
            query = [rng.choice(vocabulary + ["unseen"]) for _ in range(rng.randint(0, 8))]
            assert port.get_scores(query) == reference.get_scores(query).tolist()


def test_an_empty_corpus_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one document"):
        OkapiBm25([])
