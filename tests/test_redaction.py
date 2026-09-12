import pytest

from care.redaction import (
    MAX_EXCERPT_LENGTH,
    REDACTED_DOSAGE,
    bound_excerpt,
    contains_dosage_notation,
    redact_and_bound,
    redact_dosage,
)


@pytest.mark.parametrize(
    "text",
    [
        "50mg",
        "take 50 mg now",
        "2 tablets",
        "10 ml",
        "10мл",
        "5 мг двічі на день",
        "0.5 mg",
        "2,5 мг",
        "3 pills before bed",
    ],
)
def test_contains_dosage_notation_true_cases(text):
    assert contains_dosage_notation(text)


@pytest.mark.parametrize(
    "text",
    [
        "the blood pressure tablet",
        "the evening heart pill",
        "take your medicine",
        "call in 5 minutes",
        "room 5",
        "",
    ],
)
def test_contains_dosage_notation_false_cases(text):
    assert not contains_dosage_notation(text)


def test_redact_dosage_replaces_number_and_unit():
    assert redact_dosage("take 50mg now") == f"take {REDACTED_DOSAGE} now"


def test_redact_dosage_handles_multiple_occurrences():
    result = redact_dosage("2 tablets in the morning, 10 ml in the evening")
    assert result.count(REDACTED_DOSAGE) == 2
    assert "10" not in result
    assert "tablets" not in result


def test_redact_dosage_handles_multiline_input():
    text = "she said:\n50mg\nof the blood pressure tablet"
    result = redact_dosage(text)
    assert "50mg" not in result
    assert REDACTED_DOSAGE in result
    assert "\n" in result


def test_redact_dosage_handles_ukrainian_units():
    result = redact_dosage("випила 5 мг")
    assert "5 мг" not in result
    assert REDACTED_DOSAGE in result


def test_redact_dosage_preserves_apostrophe_text():
    text = "сім'я приїде, все добре"
    assert redact_dosage(text) == text


def test_redact_dosage_preserves_emoji():
    text = "took it ✅"
    assert redact_dosage(text) == text


def test_redact_dosage_leaves_ordinary_text_untouched():
    text = "the blood pressure tablet, all good"
    assert redact_dosage(text) == text


def test_bound_excerpt_truncates_long_input():
    text = "a" * 1000
    bounded = bound_excerpt(text)
    assert len(bounded) == MAX_EXCERPT_LENGTH
    assert bounded.endswith("…")


def test_bound_excerpt_leaves_short_input_untouched():
    text = "short message"
    assert bound_excerpt(text) == text


def test_redact_and_bound_composes_both_operations():
    text = ("took 50mg. " * 50)
    result = redact_and_bound(text)
    assert len(result) <= MAX_EXCERPT_LENGTH
    assert "50mg" not in result


def test_no_raw_dosage_value_survives_redact_and_bound():
    text = "mum said she took 2 tablets and 10 ml of syrup, всього 5 мг"
    result = redact_and_bound(text)
    for leaked in ("2 tablets", "10 ml", "5 мг"):
        assert leaked not in result
