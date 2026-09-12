from pathlib import Path

import pytest
import yaml

from care.errors import ConfigError
from care.triage import Catalogue, Term, find_matches, is_affirmative, is_negated, load_catalogue, normalise

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_normalise_lowercases_and_tokenizes():
    assert normalise("Не випила ще!") == ["не", "випила", "ще"]


def test_normalise_preserves_internal_apostrophe():
    assert normalise("сім'я приїде") == ["сім'я", "приїде"]


def test_normalise_folds_curly_apostrophe_to_straight():
    assert normalise("can’t breathe") == ["can't", "breathe"]


def test_normalise_ignores_emoji_as_tokens():
    assert normalise("ok ✅ done") == ["ok", "done"]


def test_normalise_handles_multiline_and_mixed_scripts():
    assert normalise("впала\nне можу встати") == ["впала", "не", "можу", "встати"]


def test_prefix_mode_matches_multiple_inflections():
    catalogue = Catalogue(
        language="uk",
        terms=(Term(category="fall", mode="prefix", raw="впа", tokens=("впа",)),),
    )
    for text in ("вона впала", "він впав", "вони впали", "я впаду"):
        assert find_matches(text, catalogue), text


def test_word_mode_requires_exact_token():
    catalogue = Catalogue(
        language="en",
        terms=(Term(category="yes", mode="word", raw="ok", tokens=("ok",)),),
    )
    assert find_matches("ok", catalogue)
    assert not find_matches("okay", catalogue)
    assert not find_matches("okish", catalogue)


def test_phrase_mode_requires_adjacent_order():
    catalogue = Catalogue(
        language="en",
        terms=(
            Term(
                category="cannot_get_up",
                mode="phrase",
                raw="can't get up",
                tokens=("can't", "get", "up"),
            ),
        ),
    )
    assert find_matches("i can't get up right now", catalogue)
    assert not find_matches("get up, i can't stand", catalogue)
    assert not find_matches("up i can't get", catalogue)


def test_find_matches_traces_back_to_exact_term():
    catalogue = Catalogue(
        language="uk",
        terms=(
            Term(category="fall", mode="prefix", raw="впал", tokens=("впал",)),
            Term(category="chest_pain", mode="phrase", raw="болить серце", tokens=("болить", "серце")),
        ),
    )
    matches = find_matches("впала, і болить серце", catalogue)
    categories = {m.category for m in matches}
    terms = {m.term for m in matches}
    assert categories == {"fall", "chest_pain"}
    assert terms == {"впал", "болить серце"}


def test_load_catalogue_rejects_unknown_mode(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "language: en\nentries:\n  - category: x\n    mode: fuzzy\n    terms:\n      - foo\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_catalogue(path)


def test_load_catalogue_rejects_multi_token_word_term(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "language: en\nentries:\n  - category: x\n    mode: word\n    terms:\n      - two words\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_catalogue(path)


def test_load_catalogue_rejects_empty_terms(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("language: en\nentries:\n  - category: x\n    mode: word\n    terms: []\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_catalogue(path)


def test_load_catalogue_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_catalogue(tmp_path / "does-not-exist.yaml")


def test_load_catalogue_normalises_terms_at_load_time(tmp_path):
    path = tmp_path / "cat.yaml"
    path.write_text(
        "language: uk\nentries:\n  - category: fall\n    mode: prefix\n    terms:\n      - ВПАЛ\n",
        encoding="utf-8",
    )
    catalogue = load_catalogue(path)
    assert catalogue.terms[0].tokens == ("впал",)


def test_repo_affirmatives_yes_category_is_a_string_not_boolean():
    path = CONFIG_DIR / "affirmatives.uk.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    categories = [entry["category"] for entry in raw["entries"]]
    assert "yes" in categories
    for category in categories:
        assert isinstance(category, str)
        assert category is not True
        assert category is not False


def test_no_catalogue_category_or_term_is_boolean():
    for lang in ("uk", "en"):
        for kind in ("tripwire", "affirmatives", "negatives"):
            path = CONFIG_DIR / f"{kind}.{lang}.yaml"
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            for entry in raw["entries"]:
                assert isinstance(entry["category"], str), (path, entry)
                for term in entry["terms"]:
                    assert isinstance(term, str), (path, entry, term)


def test_repo_catalogues_load_successfully():
    for lang in ("uk", "en"):
        for kind in ("tripwire", "affirmatives", "negatives"):
            load_catalogue(CONFIG_DIR / f"{kind}.{lang}.yaml")


def test_is_affirmative_matches_explicit_checkmark_emoji():
    catalogue = load_catalogue(CONFIG_DIR / "affirmatives.en.yaml")
    assert is_affirmative("✅", catalogue)


def test_is_affirmative_matches_configured_term():
    catalogue = load_catalogue(CONFIG_DIR / "affirmatives.uk.yaml")
    assert is_affirmative("так, взяла", catalogue)


def test_negation_is_detected_independently_of_affirmative_prefix_match():
    affirmatives = load_catalogue(CONFIG_DIR / "affirmatives.uk.yaml")
    negatives = load_catalogue(CONFIG_DIR / "negatives.uk.yaml")
    text = "не випила"
    assert is_affirmative(text, affirmatives)
    assert is_negated(text, negatives)


def test_is_negated_english_didnt_variants():
    catalogue = load_catalogue(CONFIG_DIR / "negatives.en.yaml")
    assert is_negated("didn't take it", catalogue)
    assert is_negated("didnt take it", catalogue)
    assert not is_negated("i took it", catalogue)
