from services.alignment import normalize_mms_word


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
