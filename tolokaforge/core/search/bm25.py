"""The ``bm25`` search backend: Okapi BM25 over the bundled corpus, in the runner process.

The second built-in retrieval (ADR-0053, change 2). A task selects it with
``initial_state.rag.backend: bm25``; it needs no stack service, so its tasks run
on the core stack, and the runner builds the trial's index at ``RegisterTrial``
from the corpus the task shipped. Everything a task can tune travels in
``backend_config`` and is validated here into :class:`Bm25BackendConfig` — the
engine never reads its keys.

* :class:`OkapiBm25` — the scorer, pure Python, no numpy at runtime. It follows
  the expression order of ``rank_bm25`` 0.2.2's ``BM25Okapi`` exactly, so its
  scores are bit-identical to the reference library's (see the class docstring).
* :func:`load_bm25_corpus` — the documents: ``{id, title, content}`` JSON files or
  ``.md`` / ``.txt`` text files, in file-name order.
* :class:`Bm25SearchBackend`, :class:`Bm25SearchIndex`,
  :class:`Bm25KnowledgeSearch` — the seam: the agent's tool schema, the trial's
  index, the agent's search in the configured rendering, and the judge's read over
  the same index returning full documents.

A built corpus is cached in-process by the corpus's content and the config, since
every trial of a task indexes the same files (:func:`clear_index_cache` empties
it). A failed search raises; it is never rendered as empty results.

Attribution: the scoring is a port of ``rank_bm25`` 0.2.2
(https://github.com/dorianbrown/rank_bm25), Apache License 2.0, Copyright (c)
2019 Dorian Brown. Only ``BM25Okapi``'s construction and ``get_scores`` are
ported; the numpy array expression is evaluated per document with Python
floats in the same operation order.
"""

from __future__ import annotations

import hashlib
import json
import math
import string
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.search.backend import (
    SearchBackendContext,
    SearchIndexBuildError,
    SearchOutcome,
)
from tolokaforge.runner.models import SearchPlane

__all__ = [
    "TOKENIZERS",
    "Bm25BackendConfig",
    "Bm25CorpusError",
    "Bm25Document",
    "Bm25DocumentsConfig",
    "Bm25JsonRender",
    "Bm25KnowledgeSearch",
    "Bm25Parameters",
    "Bm25RankingConfig",
    "Bm25SearchBackend",
    "Bm25SearchIndex",
    "Bm25TextRender",
    "CorpusFile",
    "IndexedCorpus",
    "OkapiBm25",
    "clear_index_cache",
    "corpus_fingerprint",
    "documents_from_files",
    "files_fingerprint",
    "load_bm25_corpus",
    "read_corpus_files",
]


# =============================================================================
# The scorer
# =============================================================================


class Bm25CorpusError(ValueError):
    """The corpus directory does not hold a loadable, scorable corpus; the trial is refused."""


class OkapiBm25:
    """Okapi BM25 with ``rank_bm25`` 0.2.2's ``BM25Okapi`` arithmetic, bit for bit.

    Upstream computes ``get_scores`` with numpy arrays, but every operation in its
    expression is an element-wise IEEE-754 float64 multiply, divide or add — no
    reduction — so evaluating the same expression per document with Python floats,
    in the same operation order, gives identical bits. The subtleties kept:

    * IDF is ``log(N - df + 0.5) - log(df + 0.5)``, two logarithms, not one;
    * a negative IDF (a term in more than half the documents) is replaced by
      ``epsilon * average_idf``, the average taken over the whole vocabulary — in
      the order terms first appear in the corpus — before the replacement;
    * a query term repeated is scored repeatedly, and an unseen term scores zero;
    * ``get_scores`` is one expression per document per term:
      ``idf * (tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_len / avgdl)))``,
      accumulated term by term. Reordering it changes the last bits and can
      reorder ties.

    A corpus with no document, or whose documents tokenize to no term at all, is
    refused with :class:`Bm25CorpusError`: it has no vocabulary to average an IDF
    over and an average document length of zero, on which ``rank_bm25`` divides by
    zero. A corpus where only some documents are empty is scored as upstream does.
    """

    def __init__(
        self,
        corpus: Sequence[Sequence[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
    ) -> None:
        if len(corpus) == 0:
            raise Bm25CorpusError("OkapiBm25 needs at least one document")
        self.k1 = k1
        self.b = b
        self.epsilon = epsilon
        self.corpus_size = 0
        self.avgdl = 0.0
        self.doc_freqs: list[dict[str, int]] = []
        self.idf: dict[str, float] = {}
        self.doc_len: list[int] = []
        self.average_idf = 0.0
        nd = self._initialize(corpus)
        if not nd:
            raise Bm25CorpusError(
                f"none of the {self.corpus_size} documents' indexed text tokenizes to a term, "
                "so BM25 has no vocabulary to score and an average document length of 0"
            )
        self._calc_idf(nd)

    def _initialize(self, corpus: Sequence[Sequence[str]]) -> dict[str, int]:
        nd: dict[str, int] = {}  # term -> number of documents holding it
        num_doc = 0
        for document in corpus:
            self.doc_len.append(len(document))
            num_doc += len(document)
            frequencies: dict[str, int] = {}
            for word in document:
                if word not in frequencies:
                    frequencies[word] = 0
                frequencies[word] += 1
            self.doc_freqs.append(frequencies)
            for word in frequencies:
                try:
                    nd[word] += 1
                except KeyError:
                    nd[word] = 1
            self.corpus_size += 1
        self.avgdl = num_doc / self.corpus_size
        return nd

    def _calc_idf(self, nd: dict[str, int]) -> None:
        idf_sum = 0.0
        negative_idfs: list[str] = []
        for word, freq in nd.items():
            idf = math.log(self.corpus_size - freq + 0.5) - math.log(freq + 0.5)
            self.idf[word] = idf
            idf_sum += idf
            if idf < 0:
                negative_idfs.append(word)
        self.average_idf = idf_sum / len(self.idf)
        eps = self.epsilon * self.average_idf
        for word in negative_idfs:
            self.idf[word] = eps

    def get_scores(self, query: Sequence[str]) -> list[float]:
        """One score per document, in corpus order."""
        score = [0.0] * self.corpus_size
        doc_len = self.doc_len
        k1, b, avgdl = self.k1, self.b, self.avgdl
        for q in query:
            idf = self.idf.get(q) or 0
            for i, doc in enumerate(self.doc_freqs):
                q_freq = doc.get(q) or 0
                # Upstream: idf * (q_freq * (k1 + 1) / (q_freq + k1 * (1 - b + b * doc_len / avgdl)))
                score[i] += idf * (
                    q_freq * (k1 + 1) / (q_freq + k1 * (1 - b + b * doc_len[i] / avgdl))
                )
        return score


# =============================================================================
# Tokenizers
# =============================================================================

Tokenizer = Callable[[str], list[str]]


def _whitespace_lower(text: str) -> list[str]:
    return text.lower().split()


TOKENIZERS: Mapping[str, Tokenizer] = {
    "whitespace_lower": _whitespace_lower,
}
"""The named tokenizers ``backend_config.tokenizer`` selects from.

``whitespace_lower`` is ``text.lower().split()``: lower-cased, split on runs of
whitespace, punctuation kept attached to its word. A tokenizer applies to the
documents at index time and to every query.
"""


# =============================================================================
# backend_config
# =============================================================================

DEFAULT_ITEM_TEMPLATE = "{index}. {title}\n   ID: {id}\n   Score: {score}\n   Content: {content}\n"
DEFAULT_TIMING_TEMPLATE = "\n\n[Timing: retrieval={retrieval_ms}ms, total={total_ms}ms]"

_ITEM_FIELDS = frozenset({"index", "id", "title", "score", "content", "source"})
_TIMING_FIELDS = frozenset({"retrieval_ms", "reranking_ms", "total_ms"})


def _template_fields(template: str) -> set[str]:
    """The named fields a ``str.format`` template references; a positional one is refused."""
    names: set[str] = set()
    for _literal, field_name, _spec, _conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name == "" or field_name[0].isdigit():
            raise ValueError("templates take named fields only, not positional '{}' fields")
        names.add(field_name.split(".")[0].split("[")[0])
    return names


def _refuse_unknown_fields(template: str, allowed: frozenset[str], what: str) -> str:
    unknown = sorted(_template_fields(template) - allowed)
    if unknown:
        raise ValueError(
            f"{what} references unknown fields {unknown}; it may use {sorted(allowed)}"
        )
    return template


class Bm25DocumentsConfig(BaseModel):
    """``backend_config.documents``: what the corpus directory holds, and how it is read.

    The corpus is the flat set of files in ``initial_state.rag.corpus_dir``
    (``search.documents_path`` on the wire), loaded in sorted file-name order;
    that order is the corpus order ties are broken by.

    * ``format`` — ``json``: every ``.json`` file is one document, an object with
      the keys ``id``, ``title`` and ``content`` (strings; ``id`` and ``content``
      non-empty; other keys are ignored). ``text``: every ``.md`` / ``.txt`` file
      is one document whose ``id`` and ``title`` are the file's stem and whose
      ``content`` is its text. ``auto`` (the default) reads both by extension.
      Files of any other extension are not documents and are skipped.
    * ``order`` — ``filename``: sorted by file name, the only order.
    * ``skip_prefix`` — files whose name starts with it are skipped (``_`` by
      default, so ``_README.md`` beside the documents is not indexed); the empty
      string skips nothing.
    * ``fields`` — which document fields are indexed, joined by a space in this
      order before tokenizing: ``[content]`` (the default), ``[title]`` or
      ``[title, content]``.
      The agent and the judge always read the whole ``content``.

    A duplicate ``id``, a document whose ``content`` is blank, a document whose
    indexed ``fields`` are blank (a blank ``title`` under ``fields: [title]``), a
    JSON document missing a key or holding a non-string, and a corpus loading no
    document each refuse the trial, naming the file.
    """

    model_config = {"extra": "forbid"}

    format: Literal["auto", "json", "text"] = "auto"
    order: Literal["filename"] = "filename"
    skip_prefix: str = "_"
    fields: list[Literal["title", "content"]] = Field(default_factory=lambda: ["content"])

    @field_validator("fields")
    @classmethod
    def _at_least_one_distinct_field(cls, fields: list[str]) -> list[str]:
        if not fields:
            raise ValueError("documents.fields names at least one field to index")
        if len(set(fields)) != len(fields):
            raise ValueError(f"documents.fields repeats a field: {fields}")
        return fields


class Bm25Parameters(BaseModel):
    """``backend_config.bm25``: Okapi BM25's constants, ``rank_bm25``'s defaults.

    ``k1`` is term-frequency saturation, ``b`` the document-length normalisation
    (``0`` none, ``1`` full), and ``epsilon`` the floor for a negative IDF as a
    fraction of the average IDF.
    """

    model_config = {"extra": "forbid"}

    k1: float = Field(default=1.5, ge=0.0)
    b: float = Field(default=0.75, ge=0.0, le=1.0)
    epsilon: float = Field(default=0.25, ge=0.0)


class Bm25RankingConfig(BaseModel):
    """``backend_config.ranking``: how many hits, and which.

    * ``top_k`` — how many hits a search returns, ``min(top_k, N)`` for a corpus
      of ``N`` documents: a score of zero is a hit like any other, so a query
      touching no document still returns the first ``top_k`` documents in corpus
      order. When ``agent_parameters`` exposes ``top_k`` the agent's argument
      overrides it per call.
    * ``min_score`` — keep only hits scoring at least this much (``null``, the
      default, keeps every score; ``0.0`` still keeps zero scores — a positive
      threshold is what drops the documents a query does not touch).
    * ``tie_break`` — ``corpus_order``: equal scores rank in corpus order, the
      only rule. The sort key is ``(-score, corpus_index)``.
    """

    model_config = {"extra": "forbid"}

    top_k: int = Field(default=5, ge=1)
    min_score: float | None = None
    tie_break: Literal["corpus_order"] = "corpus_order"


class Bm25JsonRender(BaseModel):
    """``backend_config.render: {kind: json}``: the agent reads one JSON object.

    With hits: ``{"results": [{"doc_id", "title", "source", "score", "text"}, …],
    "total": N, "query": …}``, ``text`` being the whole document. With none:
    ``{"message": <empty_text>, "results": [], "query": …}``. An empty query
    under ``empty_query: error``: ``{"error": <error_text>, "results": []}``.
    """

    model_config = {"extra": "forbid"}

    kind: Literal["json"] = "json"
    empty_text: str = "No relevant documents found."
    error_text: str = "Query is required"


class Bm25TextRender(BaseModel):
    """``backend_config.render: {kind: text}``: the agent reads templated text.

    Each hit is ``item_template`` formatted with ``str.format`` over the named
    fields ``{index}`` (1-based rank), ``{id}``, ``{title}``, ``{score}`` (already
    formatted with ``score_format``), ``{content}`` (the whole document) and
    ``{source}`` (the file name); a format spec may follow a name (``{content:.300}``
    cuts the document to 300 characters). Items are joined by ``separator``. No
    hits renders ``empty_text``; an empty query under ``empty_query: error``
    renders ``error_text`` alone.

    ``timing_suffix: measured`` appends ``timing_template`` formatted with the
    search's measured integer milliseconds: ``{retrieval_ms}`` (scoring and
    ranking), ``{total_ms}`` (the whole call up to the suffix) and
    ``{reranking_ms}``, which is always ``0`` — this backend has no reranking
    stage; the field exists so a template can carry that segment. ``off`` (the
    default) appends nothing. The suffix follows the hits and the no-hits text,
    never the error text.

    The defaults render, for each hit::

        1. <title>
           ID: <id>
           Score: 0.1234
           Content: <content>

    joined by one newline.
    """

    model_config = {"extra": "forbid"}

    kind: Literal["text"]
    item_template: str = DEFAULT_ITEM_TEMPLATE
    separator: str = "\n"
    score_format: str = ".4f"
    timing_suffix: Literal["off", "measured"] = "off"
    timing_template: str = DEFAULT_TIMING_TEMPLATE
    empty_text: str = "No results found."
    error_text: str = "Error: the query must not be empty."

    @field_validator("item_template")
    @classmethod
    def _item_template_fields(cls, template: str) -> str:
        return _refuse_unknown_fields(template, _ITEM_FIELDS, "render.item_template")

    @field_validator("timing_template")
    @classmethod
    def _timing_template_fields(cls, template: str) -> str:
        return _refuse_unknown_fields(template, _TIMING_FIELDS, "render.timing_template")

    @field_validator("score_format")
    @classmethod
    def _score_format_formats_a_float(cls, score_format: str) -> str:
        try:
            format(0.0, score_format)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"render.score_format {score_format!r} cannot format a float: {exc}")
        return score_format


class Bm25BackendConfig(BaseModel):
    """``initial_state.rag.backend_config`` for the ``bm25`` backend, every key defaulted.

    * ``documents`` — :class:`Bm25DocumentsConfig`.
    * ``tokenizer`` — a name from :data:`TOKENIZERS` (``whitespace_lower``).
    * ``bm25`` — :class:`Bm25Parameters`.
    * ``ranking`` — :class:`Bm25RankingConfig`.
    * ``empty_query`` — what a blank query (empty or whitespace) gets:
      ``no_results`` renders the no-hits text; ``error`` renders the error text.
      Either way the agent reads text and nothing raises — a blank query is the
      agent's mistake, not an outage.
    * ``render`` — :class:`Bm25JsonRender` (the default) or :class:`Bm25TextRender`,
      chosen by ``kind``.
    * ``agent_parameters`` — which of ``query`` and ``top_k`` the agent's tool
      schema exposes; ``query`` is mandatory. An argument the schema does not
      expose is ignored.

    Unknown keys are refused, as is a value outside its range, before any trial.
    """

    model_config = {"extra": "forbid"}

    documents: Bm25DocumentsConfig = Field(default_factory=Bm25DocumentsConfig)
    tokenizer: str = "whitespace_lower"
    bm25: Bm25Parameters = Field(default_factory=Bm25Parameters)
    ranking: Bm25RankingConfig = Field(default_factory=Bm25RankingConfig)
    empty_query: Literal["no_results", "error"] = "no_results"
    render: Bm25JsonRender | Bm25TextRender = Field(
        default_factory=Bm25JsonRender, discriminator="kind"
    )
    agent_parameters: list[Literal["query", "top_k"]] = Field(default_factory=lambda: ["query"])

    @field_validator("tokenizer")
    @classmethod
    def _a_registered_tokenizer(cls, name: str) -> str:
        if name not in TOKENIZERS:
            raise ValueError(f"unknown tokenizer {name!r}; known: {sorted(TOKENIZERS)}")
        return name

    @field_validator("agent_parameters")
    @classmethod
    def _query_is_exposed(cls, parameters: list[str]) -> list[str]:
        if "query" not in parameters:
            raise ValueError("agent_parameters must include 'query'; the runner hands it over")
        if len(set(parameters)) != len(parameters):
            raise ValueError(f"agent_parameters repeats a parameter: {parameters}")
        return parameters

    def fingerprint(self) -> str:
        """A digest of the whole config, one half of the index cache's key."""
        dumped = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


# =============================================================================
# The corpus
# =============================================================================


@dataclass(frozen=True)
class Bm25Document:
    """One document: its id and title, the whole text, and the file it came from."""

    id: str
    title: str
    content: str
    source: str

    def indexed_text(self, fields: Sequence[str]) -> str:
        return " ".join(getattr(self, name) for name in fields)


_TEXT_SUFFIXES = frozenset({".md", ".txt"})
_JSON_SUFFIX = ".json"


@dataclass(frozen=True)
class CorpusFile:
    """One regular file of the corpus directory, read once: its name and its bytes."""

    name: str
    data: bytes

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    def text(self) -> str:
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Bm25CorpusError(f"{self.name}: not UTF-8 text: {exc}") from exc


def read_corpus_files(corpus_dir: Path) -> tuple[CorpusFile, ...]:
    """Every regular file of ``corpus_dir`` in file-name order, each read exactly once.

    The fingerprint and the documents are both derived from these bytes, so a
    build opens each file once.

    Raises:
        Bm25CorpusError: the directory does not exist.
    """
    if not corpus_dir.is_dir():
        raise Bm25CorpusError(f"corpus directory {corpus_dir} does not exist")
    return tuple(
        CorpusFile(name=path.name, data=path.read_bytes())
        for path in sorted(corpus_dir.iterdir(), key=lambda p: p.name)
        if path.is_file()
    )


def _document_from_json(file: CorpusFile) -> Bm25Document:
    try:
        payload = json.loads(file.text())
    except ValueError as exc:
        raise Bm25CorpusError(f"{file.name}: not a JSON document: {exc}") from exc
    if not isinstance(payload, dict):
        raise Bm25CorpusError(
            f"{file.name}: a JSON document is one object with id, title and content, "
            f"got {type(payload).__name__}"
        )
    missing = [key for key in ("id", "title", "content") if key not in payload]
    if missing:
        raise Bm25CorpusError(f"{file.name}: JSON document is missing {missing}")
    values: dict[str, str] = {}
    for key in ("id", "title", "content"):
        value = payload[key]
        if not isinstance(value, str):
            raise Bm25CorpusError(
                f"{file.name}: JSON document's {key!r} is {type(value).__name__}, not a string"
            )
        values[key] = value
    if not values["id"].strip():
        raise Bm25CorpusError(f"{file.name}: JSON document's 'id' is blank")
    return Bm25Document(
        id=values["id"], title=values["title"], content=values["content"], source=file.name
    )


def _document_from_text(file: CorpusFile) -> Bm25Document:
    return Bm25Document(id=file.stem, title=file.stem, content=file.text(), source=file.name)


def _reader_for(
    file: CorpusFile, document_format: str
) -> Callable[[CorpusFile], Bm25Document] | None:
    if document_format in ("auto", "json") and file.suffix == _JSON_SUFFIX:
        return _document_from_json
    if document_format in ("auto", "text") and file.suffix in _TEXT_SUFFIXES:
        return _document_from_text
    return None


def documents_from_files(
    files: Sequence[CorpusFile], config: Bm25DocumentsConfig, *, corpus_dir: Path
) -> tuple[Bm25Document, ...]:
    """The documents among ``files`` in corpus order, per :class:`Bm25DocumentsConfig`.

    Raises:
        Bm25CorpusError: a document is malformed, blank or blank in its indexed
            fields, two documents share an id, or nothing loads. ``corpus_dir`` names
            the directory in the refusal.
    """
    documents: list[Bm25Document] = []
    seen: dict[str, str] = {}
    for file in files:
        if config.skip_prefix and file.name.startswith(config.skip_prefix):
            continue
        reader = _reader_for(file, config.format)
        if reader is None:
            continue
        document = reader(file)
        if not document.content.strip():
            raise Bm25CorpusError(f"{file.name}: document {document.id!r} has no content")
        if not document.indexed_text(config.fields).strip():
            raise Bm25CorpusError(
                f"{file.name}: document {document.id!r} has no text in its indexed fields "
                f"{list(config.fields)}"
            )
        if document.id in seen:
            raise Bm25CorpusError(
                f"{file.name}: document id {document.id!r} is already used by {seen[document.id]}"
            )
        seen[document.id] = file.name
        documents.append(document)
    if not documents:
        raise Bm25CorpusError(
            f"corpus directory {corpus_dir} holds no document (format {config.format!r}, "
            f"skip_prefix {config.skip_prefix!r})"
        )
    return tuple(documents)


def load_bm25_corpus(corpus_dir: Path, config: Bm25DocumentsConfig) -> tuple[Bm25Document, ...]:
    """The documents of ``corpus_dir`` in corpus order, per :class:`Bm25DocumentsConfig`.

    Raises:
        Bm25CorpusError: the directory is missing, a document is malformed or
            blank, two documents share an id, or nothing loads.
    """
    return documents_from_files(read_corpus_files(corpus_dir), config, corpus_dir=corpus_dir)


def files_fingerprint(files: Sequence[CorpusFile]) -> str:
    """A digest of every file's name and bytes, the other half of the cache key."""
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(file.data).to_bytes(8, "big"))
        digest.update(file.data)
    return digest.hexdigest()


def corpus_fingerprint(corpus_dir: Path) -> str:
    """:func:`files_fingerprint` over the directory's files, read for this call."""
    return files_fingerprint(read_corpus_files(corpus_dir))


# =============================================================================
# The built corpus and its in-process cache
# =============================================================================


@dataclass(frozen=True)
class IndexedCorpus:
    """A corpus with its scorer: what every trial over the same files shares."""

    documents: tuple[Bm25Document, ...]
    scorer: OkapiBm25
    tokenizer: Tokenizer
    cache_key: tuple[str, str]

    def rank(
        self, query: str, *, top_k: int, min_score: float | None
    ) -> list[tuple[Bm25Document, float]]:
        """The top ``top_k`` documents by score, ties in corpus order, in rank order."""
        scores = self.scorer.get_scores(self.tokenizer(query))
        order = sorted(range(len(self.documents)), key=lambda i: (-scores[i], i))
        ranked = [(self.documents[i], scores[i]) for i in order]
        if min_score is not None:
            ranked = [(document, score) for document, score in ranked if score >= min_score]
        return ranked[:top_k]


_CACHE_LIMIT = 16
_cache: OrderedDict[tuple[str, str], IndexedCorpus] = OrderedDict()
_cache_lock = threading.Lock()


def clear_index_cache() -> None:
    """Drop every cached corpus (tests, and a process that wants to re-read its files)."""
    with _cache_lock:
        _cache.clear()


def _indexed_corpus(corpus_dir: Path, config: Bm25BackendConfig) -> tuple[IndexedCorpus, bool]:
    """The built corpus for ``(corpus files, config)``, and whether it came from the cache.

    The key is the corpus's content, not its path: every trial of a task extracts
    the same files to a fresh directory. The sixteen most recently used corpora stay.
    """
    files = read_corpus_files(corpus_dir)
    key = (files_fingerprint(files), config.fingerprint())
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached, True
    documents = documents_from_files(files, config.documents, corpus_dir=corpus_dir)
    tokenizer = TOKENIZERS[config.tokenizer]
    scorer = OkapiBm25(
        [tokenizer(document.indexed_text(config.documents.fields)) for document in documents],
        k1=config.bm25.k1,
        b=config.bm25.b,
        epsilon=config.bm25.epsilon,
    )
    built = IndexedCorpus(documents=documents, scorer=scorer, tokenizer=tokenizer, cache_key=key)
    with _cache_lock:
        _cache[key] = built
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return built, False


# =============================================================================
# The index: the agent's search and the judge's
# =============================================================================


def _hit(document: Bm25Document, score: float) -> SearchHit:
    # ``source`` is the file name and ``title`` the document's own: the judge's
    # search_kb shows it, and the remote grader's KBSearch carries it.
    return SearchHit(
        doc_id=document.id,
        source=document.source,
        score=score,
        text=document.content,
        title=document.title,
    )


def _milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


class Bm25SearchIndex:
    """One trial's BM25 index: the agent's search, rendered, and the judge's read."""

    def __init__(
        self, corpus: IndexedCorpus, config: Bm25BackendConfig, context: SearchBackendContext
    ) -> None:
        self.corpus = corpus
        self.config = config
        self._tool_name = context.tool_name
        self._logger = context.logger

    def rank(self, query: str, top_k: int) -> list[tuple[Bm25Document, float]]:
        """The ranking both the agent's search and the judge's read: the configured
        ``min_score`` and tie-break over ``top_k`` hits."""
        return self.corpus.rank(query, top_k=top_k, min_score=self.config.ranking.min_score)

    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome:
        """Answer the agent's call. In-process, so ``budget_s`` bounds nothing here.

        Raises:
            TypeError: ``query`` is not a string.
            ValueError: an exposed ``top_k`` argument is not a positive integer.
        """
        started = time.perf_counter()
        if not isinstance(query, str):
            raise TypeError(
                f"{self._tool_name}: query must be a string, got {type(query).__name__}"
            )
        top_k = self._top_k(arguments)
        render = self.config.render
        if not query.strip():
            if self.config.empty_query == "error":
                self._logger.debug(f"{self._tool_name}: blank query, rendering the error text")
                return SearchOutcome(hits=(), rendered=_render_error(render))
            self._logger.debug(f"{self._tool_name}: blank query, rendering no results")
            return SearchOutcome(hits=(), rendered=_render(render, query, [], started, 0.0))
        ranked = self.rank(query, top_k)
        retrieval_s = time.perf_counter() - started
        self._logger.debug(
            f"{self._tool_name}: query={query[:50]!r} top_k={top_k} hits={len(ranked)} "
            f"retrieval_ms={_milliseconds(retrieval_s)}"
        )
        hits = tuple(_hit(document, score) for document, score in ranked)
        return SearchOutcome(
            hits=hits, rendered=_render(render, query, ranked, started, retrieval_s)
        )

    def knowledge_search(self) -> Bm25KnowledgeSearch:
        """The judge's read over this same index, returning whole documents."""
        return Bm25KnowledgeSearch(self)

    def _top_k(self, arguments: Mapping[str, Any]) -> int:
        if "top_k" not in self.config.agent_parameters or arguments.get("top_k") is None:
            return self.config.ranking.top_k
        top_k = arguments["top_k"]
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"{self._tool_name}: top_k must be a positive integer, got {top_k!r}")
        return top_k


class Bm25KnowledgeSearch:
    """The judge's :class:`~tolokaforge.core.grading.kb_search.KnowledgeSearch` over the index.

    Same ranking as the agent's tool; ``alpha`` is a hybrid weight this backend has
    none of and ignores; a blank query finds nothing. ``text`` is the whole document.
    """

    def __init__(self, index: Bm25SearchIndex) -> None:
        self.index = index

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
        if not query.strip():
            return []
        return [_hit(document, score) for document, score in self.index.rank(query, top_k)]


# =============================================================================
# Rendering
# =============================================================================


def _render(
    render: Bm25JsonRender | Bm25TextRender,
    query: str,
    ranked: Sequence[tuple[Bm25Document, float]],
    started: float,
    retrieval_s: float,
) -> str:
    if isinstance(render, Bm25JsonRender):
        return _render_json(render, query, ranked)
    return _render_text(render, ranked, started, retrieval_s)


def _render_error(render: Bm25JsonRender | Bm25TextRender) -> str:
    if isinstance(render, Bm25JsonRender):
        return json.dumps({"error": render.error_text, "results": []}, ensure_ascii=False)
    return render.error_text


def _render_json(
    render: Bm25JsonRender, query: str, ranked: Sequence[tuple[Bm25Document, float]]
) -> str:
    if not ranked:
        return json.dumps(
            {"message": render.empty_text, "results": [], "query": query}, ensure_ascii=False
        )
    results = [
        {
            "doc_id": document.id,
            "title": document.title,
            "source": document.source,
            "score": score,
            "text": document.content,
        }
        for document, score in ranked
    ]
    return json.dumps(
        {"results": results, "total": len(results), "query": query}, ensure_ascii=False
    )


def _render_text(
    render: Bm25TextRender,
    ranked: Sequence[tuple[Bm25Document, float]],
    started: float,
    retrieval_s: float,
) -> str:
    items = [
        render.item_template.format(
            index=position,
            id=document.id,
            title=document.title,
            score=format(score, render.score_format),
            content=document.content,
            source=document.source,
        )
        for position, (document, score) in enumerate(ranked, start=1)
    ]
    body = render.separator.join(items) if items else render.empty_text
    if render.timing_suffix == "off":
        return body
    total_s = time.perf_counter() - started
    suffix = render.timing_template.format(
        retrieval_ms=_milliseconds(retrieval_s),
        reranking_ms=0,
        total_ms=_milliseconds(total_s),
    )
    return body + suffix


# =============================================================================
# The backend
# =============================================================================


class Bm25SearchBackend:
    """The ``bm25`` :class:`~tolokaforge.core.search.backend.SearchBackend`."""

    name = SearchPlane.BM25.value
    stack_service: str | None = None

    def __init__(self, context: SearchBackendContext) -> None:
        try:
            self.config = Bm25BackendConfig.model_validate(dict(context.backend_config))
        except ValidationError as exc:
            raise ValueError(f"search backend {self.name!r} refused backend_config: {exc}") from exc
        self._context = context

    def tool_parameters(self) -> dict[str, Any]:
        """``query``, plus ``top_k`` when ``agent_parameters`` exposes it; a fresh copy."""
        properties: dict[str, Any] = {
            "query": {
                "type": "string",
                "description": "Search query to find relevant documents",
            }
        }
        if "top_k" in self.config.agent_parameters:
            default = self.config.ranking.top_k
            properties["top_k"] = {
                "type": "integer",
                "description": f"Number of results to return (default: {default})",
                "default": default,
                "minimum": 1,
            }
        return {
            "type": "object",
            "properties": properties,
            "required": ["query"],
            "additionalProperties": False,
        }

    async def build_index(self, corpus_dir: Path | None) -> Bm25SearchIndex:
        """Load and score the trial's corpus, or take it from the in-process cache.

        Raises:
            RuntimeError: the context is trial-less; only the runner builds an index.
            SearchIndexBuildError: no corpus is declared, or it does not load or
                cannot be scored — the refusal ``RegisterTrial`` returns.
        """
        trial_id = self._context.trial_id
        if trial_id is None:
            raise RuntimeError(
                f"search backend {self.name!r} was asked to build an index from a trial-less "
                "context; only the runner builds one, at RegisterTrial"
            )
        if corpus_dir is None:
            raise SearchIndexBuildError(
                f"Trial {trial_id}: search backend {self.name!r} needs a corpus, and the task "
                "declares no documents_path"
            )
        try:
            corpus, cached = _indexed_corpus(corpus_dir, self.config)
        except Bm25CorpusError as exc:
            raise SearchIndexBuildError(
                f"Trial {trial_id}: search backend {self.name!r} cannot index {corpus_dir}: {exc}"
            ) from exc
        self._context.logger.info(
            f"Trial {trial_id}: bm25 index over {len(corpus.documents)} documents "
            f"({'from the cache' if cached else 'built'})",
            extra={"trial_id": trial_id, "documents_path": str(corpus_dir)},
        )
        return Bm25SearchIndex(corpus, self.config, self._context)


def _bm25_backend_factory(context: SearchBackendContext) -> Bm25SearchBackend:
    """The ``tolokaforge.search_backends`` entry point for ``bm25``."""
    return Bm25SearchBackend(context)
