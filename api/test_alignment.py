import pytest

from services.alignment import (
    AlignmentUnavailable,
    WordTiming,
    map_words_to_sentences,
    normalize_mms_word,
)


def test_lowercases_and_strips_punctuation():
    assert normalize_mms_word("Word,") == "word"
    assert normalize_mms_word("(origin)") == "origin"


def test_keeps_apostrophe():
    assert normalize_mms_word("don't") == "don't"


def test_strips_hyphen_to_single_token():
    # num2words year form for 1888 is "eighty-eight"; one aligner word.
    assert normalize_mms_word("eighty-eight") == "eightyeight"


def test_bare_digit_normalizes_to_empty():
    # Not expanded by normalize_inline; signals the caller to fall back.
    assert normalize_mms_word("5") == ""
    assert normalize_mms_word("—") == ""


def _wt(seq):
    # seq: list of (start, end) → WordTiming list with dummy words
    return [WordTiming(word=f"w{i}", start_s=s, end_s=e) for i, (s, e) in enumerate(seq)]


def test_maps_counts_to_first_and_last_word_spans():
    words = _wt([(0.0, 0.5), (0.5, 1.0), (1.0, 1.4), (1.4, 2.0)])
    # two sentences: 1 word, then 3 words
    spans = map_words_to_sentences(words, [1, 3])
    assert spans == [(0.0, 0.5), (0.5, 2.0)]


def test_single_sentence_spans_all_words():
    words = _wt([(0.2, 0.6), (0.6, 1.1)])
    assert map_words_to_sentences(words, [2]) == [(0.2, 1.1)]


def test_count_mismatch_raises():
    words = _wt([(0.0, 0.5), (0.5, 1.0)])
    with pytest.raises(AlignmentUnavailable):
        map_words_to_sentences(words, [1, 1, 1])  # sum 3 != 2


def test_zero_count_raises():
    words = _wt([(0.0, 0.5)])
    with pytest.raises(AlignmentUnavailable):
        map_words_to_sentences(words, [0, 1])
