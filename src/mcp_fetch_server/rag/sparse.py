"""Lexical (sparse) vectors for hybrid retrieval.

Dense vectors are good at meaning and bad at exact tokens. A query for a
specific error code, API name, version number or transliterated technical term
often fails against a purely semantic index, because the embedding puts it
near everything that talks about the same topic instead of the passage that
actually contains the string. So every chunk is indexed twice, and the two
result lists are fused.

The sparse side is plain term frequency; Qdrant applies IDF itself via the
collection's ``Modifier.IDF``, so no corpus statistics need to be maintained
here. Term ids are a stable hash of the term, which means the vocabulary never
has to be stored or migrated.

Persian normalisation is not optional here. The same word is routinely written
with Arabic yeh or Persian yeh, with or without ZWNJ, with or without
diacritics, and with Persian, Arabic-Indic or ASCII digits. Without folding
those together, lexical search on a Persian corpus misses most of its matches.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

# Characters that are dropped entirely: Arabic diacritics (harakat), tatweel,
# and the bidi/format marks that get pasted in from Word and PDFs.
_STRIP_RE = re.compile(r"[ً-ْـ‎‏‪-‮⁦-⁩]")

# ZWNJ joins Persian compounds. Splitting on it means "می‌رود" and "میرود"
# both yield the same tokens, which is what a lexical match needs.
_ZWNJ = "‌"

_DIGIT_MAP = {
    **{chr(0x0660 + index): str(index) for index in range(10)},  # Arabic-Indic
    **{chr(0x06F0 + index): str(index) for index in range(10)},  # Extended (Persian)
}

_LETTER_MAP = {
    "ي": "ی",  # Arabic yeh -> Persian yeh
    "ى": "ی",  # alef maksura -> Persian yeh
    "ك": "ک",  # Arabic kaf -> Persian keheh
    "أ": "ا",  # alef with hamza above -> alef
    "إ": "ا",  # alef with hamza below -> alef
    "آ": "ا",  # alef with madda -> alef
    "ة": "ه",  # teh marbuta -> heh
    "ۀ": "ه",  # heh with yeh above -> heh
}

_TRANSLATION = str.maketrans({**_DIGIT_MAP, **_LETTER_MAP})

# Word characters across scripts, plus internal dots/hyphens so that
# "bge-m3", "v1.2" and "fetch_url" survive as single terms.
_TOKEN_RE = re.compile(r"[^\W_]+(?:[._-][^\W_]+)*", re.UNICODE)

# Stop words are only removed for very common function words in the two
# languages this corpus mixes. An aggressive list would hurt: with IDF
# weighting, common terms are already cheap.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
        "for", "on", "with", "as", "by", "at", "it", "this", "that", "be", "from",
        "و", "در", "به", "از", "که", "را", "با", "این", "است", "برای", "بر", "تا",
        "یا", "هم", "می", "شد", "شده", "های", "ها",
    }
)

MAX_TERM_LENGTH = 40


@dataclass(frozen=True, slots=True)
class SparseVector:
    """Term ids and their weights, in Qdrant's sparse-vector shape."""

    indices: list[int]
    values: list[float]

    def __len__(self) -> int:
        return len(self.indices)

    def __bool__(self) -> bool:
        return bool(self.indices)


def normalize(text: str) -> str:
    """Fold script variants so the same word matches itself."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    folded = _STRIP_RE.sub("", folded)
    folded = folded.replace(_ZWNJ, " ")
    return folded.translate(_TRANSLATION).lower()


def tokenize(text: str, *, remove_stopwords: bool = True) -> list[str]:
    """Split normalised text into lexical terms."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalize(text)):
        term = match.group(0)
        if len(term) > MAX_TERM_LENGTH:
            term = term[:MAX_TERM_LENGTH]
        # Single Latin characters carry no signal; single CJK characters do.
        if len(term) < 2 and term.isascii():
            continue
        if remove_stopwords and term in _STOPWORDS:
            continue
        tokens.append(term)
    return tokens


def term_id(term: str) -> int:
    """Stable 32-bit id for a term.

    Python's built-in hash is salted per process, so it cannot be used: ids
    written during ingestion would not match ids computed at query time in a
    later run.
    """
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def encode(text: str, *, remove_stopwords: bool = True) -> SparseVector:
    """Build a term-frequency sparse vector for one piece of text.

    Raw counts are emitted rather than a full BM25 weight: the collection is
    created with Qdrant's IDF modifier, which applies the corpus statistics at
    query time. That keeps ingestion stateless, and means adding documents
    never invalidates weights already written.
    """
    counts: dict[int, float] = {}
    for token in tokenize(text, remove_stopwords=remove_stopwords):
        identifier = term_id(token)
        counts[identifier] = counts.get(identifier, 0.0) + 1.0

    if not counts:
        return SparseVector(indices=[], values=[])

    # Sub-linear term frequency: a term appearing twenty times in one passage
    # does not make it twenty times more relevant.
    items = sorted(counts.items())
    return SparseVector(
        indices=[identifier for identifier, _ in items],
        values=[1.0 + _log2(count) for _, count in items],
    )


def _log2(value: float) -> float:
    import math

    return math.log2(value) if value > 0 else 0.0
