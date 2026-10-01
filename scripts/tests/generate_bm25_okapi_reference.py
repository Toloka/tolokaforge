#!/usr/bin/env python
"""Regenerate ``tests/fixtures/bm25_okapi_reference.json`` with ``rank_bm25`` 0.2.2.

The fixture pins the scores the reference library gives on synthetic corpora, so
``tests/unit/test_bm25_okapi_parity.py`` can check :class:`OkapiBm25` is
bit-identical to it where ``rank_bm25`` is not installed. The corpora are
generated here from fixed seeds and written out with the scores, so the fixture
is self-contained: the test reads corpora, queries and expected scores from it
and never has to reproduce the generator.

Cases cover what the port must get right: hundreds of documents, repeated query
tokens, tokens no document holds, terms in more than half the documents (a
negative IDF, floored at ``epsilon * average_idf``), a vocabulary where every
IDF is negative, non-ASCII tokens, an empty query, a one-document corpus, and
non-default ``k1`` / ``b`` / ``epsilon``.

Run from the repository root::

    .venv/bin/python scripts/tests/generate_bm25_okapi_reference.py

It refuses to run against any other ``rank_bm25`` version: the fixture states
which library it reproduces.
"""

from __future__ import annotations

import importlib.metadata
import json
import random
import sys
from pathlib import Path

REFERENCE_VERSION = "0.2.2"
FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "bm25_okapi_reference.json"


def _corpus(rng: random.Random, vocabulary: list[str], size: int, length: tuple[int, int]):
    return [[rng.choice(vocabulary) for _ in range(rng.randint(*length))] for _ in range(size)]


def _queries(rng: random.Random, vocabulary: list[str], count: int, length: tuple[int, int]):
    unseen = ["unseen", "никогда", "zzz"]
    pool = vocabulary + unseen
    queries = [[rng.choice(pool) for _ in range(rng.randint(*length))] for _ in range(count)]
    queries.append([])  # an empty query scores every document zero
    queries.append([vocabulary[0], vocabulary[0], vocabulary[0]])  # a repeated token
    queries.append(unseen)  # nothing the corpus holds
    return queries


def build_cases() -> list[dict]:
    """The cases, in a fixed order, from fixed seeds."""
    cases: list[dict] = []

    rng = random.Random(2026_01)
    wide = [f"term{i}" for i in range(80)]
    cases.append(
        {
            "name": "wide vocabulary, 300 documents, defaults",
            "k1": 1.5,
            "b": 0.75,
            "epsilon": 0.25,
            "corpus": _corpus(rng, wide, 300, (3, 40)),
            "queries": _queries(rng, wide, 12, (1, 8)),
        }
    )

    rng = random.Random(2026_02)
    narrow = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    cases.append(
        {
            "name": "six terms over 250 documents: every term in more than half, negative IDFs",
            "k1": 1.5,
            "b": 0.75,
            "epsilon": 0.25,
            "corpus": _corpus(rng, narrow, 250, (8, 30)),
            "queries": _queries(rng, narrow, 10, (1, 6)),
        }
    )

    rng = random.Random(2026_03)
    mixed = [f"t{i}" for i in range(30)] + ["the", "of", "and", "to"] * 1
    skewed = []
    for _ in range(200):
        doc = ["the", "of"] * rng.randint(1, 4) + [
            rng.choice(mixed) for _ in range(rng.randint(1, 20))
        ]
        rng.shuffle(doc)
        skewed.append(doc)
    cases.append(
        {
            "name": "stop-words in every document beside a long tail, non-default constants",
            "k1": 1.2,
            "b": 0.5,
            "epsilon": 0.1,
            "corpus": skewed,
            "queries": _queries(rng, mixed, 10, (1, 7)),
        }
    )

    rng = random.Random(2026_04)
    unicode = [
        "вода",
        "огонь",
        "земля",
        "воздух",
        "ñandú",
        "straße",
        "日本語",
        "東京",
        "café",
        "naïve",
    ]
    cases.append(
        {
            "name": "non-ASCII tokens, 120 documents",
            "k1": 1.5,
            "b": 0.75,
            "epsilon": 0.25,
            "corpus": _corpus(rng, unicode, 120, (1, 15)),
            "queries": _queries(rng, unicode, 8, (1, 5)),
        }
    )

    cases.append(
        {
            "name": "one document",
            "k1": 1.5,
            "b": 0.75,
            "epsilon": 0.25,
            "corpus": [["only", "one", "document", "one"]],
            "queries": [["one"], ["only", "one"], ["missing"], []],
        }
    )

    cases.append(
        {
            "name": "b at the ends of its range",
            "k1": 2.0,
            "b": 0.0,
            "epsilon": 0.25,
            "corpus": [["x", "y"], ["x"], ["y", "y", "z"], ["w"] * 10],
            "queries": [["x"], ["y", "z"], ["w", "x", "y", "z"]],
        }
    )
    cases.append(
        {
            "name": "b at one",
            "k1": 2.0,
            "b": 1.0,
            "epsilon": 0.5,
            "corpus": [["x", "y"], ["x"], ["y", "y", "z"], ["w"] * 10],
            "queries": [["x"], ["y", "z"], ["w", "x", "y", "z"]],
        }
    )
    return cases


def main() -> int:
    installed = importlib.metadata.version("rank_bm25")
    if installed != REFERENCE_VERSION:
        print(
            f"rank_bm25 {installed} is installed; the fixture reproduces {REFERENCE_VERSION}",
            file=sys.stderr,
        )
        return 1
    from rank_bm25 import BM25Okapi

    cases = build_cases()
    for case in cases:
        reference = BM25Okapi(case["corpus"], k1=case["k1"], b=case["b"], epsilon=case["epsilon"])
        case["expected"] = [reference.get_scores(query).tolist() for query in case["queries"]]
    FIXTURE.write_text(
        json.dumps(
            {"rank_bm25_version": REFERENCE_VERSION, "cases": cases},
            ensure_ascii=False,
            indent=None,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    documents = sum(len(case["corpus"]) for case in cases)
    print(f"wrote {FIXTURE} ({len(cases)} cases, {documents} documents)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
