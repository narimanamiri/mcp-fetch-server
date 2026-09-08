"""Tests for lexical sparse vectors and multilingual normalisation."""

from __future__ import annotations

from mcp_fetch_server.rag.sparse import (
    encode,
    normalize,
    term_id,
    tokenize,
)

# ---------------------------------------------------------- normalisation


def test_normalize_lowercases_and_keeps_ascii():
    assert normalize("Hello World") == "hello world"


def test_normalize_folds_arabic_and_persian_yeh():
    """The same Persian word is routinely typed with either yeh; lexical
    search must treat them as one term."""
    assert normalize("بازیابی") == normalize("بازيابي")


def test_normalize_folds_kaf():
    assert normalize("کتاب") == normalize("كتاب")


def test_normalize_folds_alef_variants():
    assert normalize("آب") == normalize("اب")
    assert normalize("أحمد") == normalize("احمد")


def test_normalize_strips_diacritics():
    assert normalize("کِتاب") == normalize("کتاب")


def test_normalize_converts_persian_and_arabic_digits():
    assert normalize("۱۲۳") == "123"
    assert normalize("٤٥٦") == "456"


def test_normalize_splits_on_zwnj():
    """ZWNJ joins Persian compounds; splitting means the spaced and unspaced
    spellings produce the same terms."""
    assert tokenize("می‌رود") == tokenize("می رود")


def test_normalize_removes_bidi_marks():
    assert normalize("test‎‏word") == "testword"


def test_normalize_empty():
    assert normalize("") == ""


# ------------------------------------------------------------ tokenizing


def test_tokenize_splits_on_punctuation():
    assert tokenize("Hello, world! Retrieval?") == ["hello", "world", "retrieval"]


def test_tokenize_keeps_technical_identifiers_whole():
    assert "bge-m3" in tokenize("We used bge-m3 for embeddings")
    assert "fetch_url" in tokenize("Call fetch_url now")
    assert "v1.2" in tokenize("Release v1.2 shipped")


def test_tokenize_drops_stopwords():
    tokens = tokenize("the quick brown fox and the dog")
    assert "the" not in tokens
    assert "and" not in tokens
    assert "quick" in tokens


def test_tokenize_drops_persian_stopwords():
    tokens = tokenize("این سند در مورد بازیابی است")
    assert "این" not in tokens
    assert "است" not in tokens
    assert "بازیابی" in tokens


def test_tokenize_can_keep_stopwords():
    assert "the" in tokenize("the fox", remove_stopwords=False)


def test_tokenize_drops_single_ascii_characters():
    assert tokenize("a b c word") == ["word"]


def test_tokenize_truncates_absurd_terms():
    token = tokenize("x" * 200)[0]
    assert len(token) == 40


def test_tokenize_empty_and_symbolic():
    assert tokenize("") == []
    assert tokenize("!!! ??? ...") == []


# ------------------------------------------------------------- term ids


def test_term_id_is_stable_and_in_range():
    first = term_id("retrieval")
    assert first == term_id("retrieval")
    assert 0 <= first < 2**32
    assert term_id("retrieval") != term_id("retrieva")


def test_term_id_is_stable_across_processes():
    """Ids are written at ingestion and recomputed at query time in another
    run, so a salted hash would silently break every lexical match."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mcp_fetch_server.rag.sparse import term_id; print(term_id('retrieval'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(result.stdout.strip()) == term_id("retrieval")


# -------------------------------------------------------------- encoding


def test_encode_produces_sorted_indices_and_values():
    vector = encode("retrieval systems index documents")
    assert len(vector) == 4
    assert vector.indices == sorted(vector.indices)
    assert len(vector.values) == len(vector.indices)
    assert all(value > 0 for value in vector.values)


def test_encode_weights_repeats_sublinearly():
    once = encode("retrieval")
    twice = encode("retrieval retrieval")
    four = encode("retrieval retrieval retrieval retrieval")
    assert twice.values[0] > once.values[0]
    # Four occurrences must not be four times the weight of one.
    assert four.values[0] < 4 * once.values[0]


def test_encode_empty_text_is_falsy():
    vector = encode("")
    assert not vector
    assert vector.indices == []


def test_encode_stopwords_only_is_empty():
    assert not encode("the and of to")


def test_encode_matches_across_persian_spellings():
    """Two spellings of the same sentence must produce the same vector."""
    first = encode("بازیابی اطلاعات چندزبانه")
    second = encode("بازيابي اطلاعات چندزبانه")
    assert first.indices == second.indices


def test_encode_is_deterministic():
    text = "hybrid retrieval combines dense and sparse"
    assert encode(text) == encode(text)


def test_encode_query_and_document_share_terms():
    document = encode("The bge-m3 model produces multilingual embeddings")
    query = encode("bge-m3 multilingual")
    assert set(query.indices) & set(document.indices)
