from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

from care.errors import ConfigError

_MODES = ("prefix", "word", "phrase")
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})
_TOKEN_RE = re.compile(r"\w+(?:'\w+)*", re.UNICODE)
_EXPLICIT_AFFIRMATIVE_SYMBOLS = ("✅",)


def normalise(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).translate(_APOSTROPHES).casefold()
    return _TOKEN_RE.findall(folded)


@dataclass(frozen=True)
class Term:
    category: str
    mode: str
    raw: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class Catalogue:
    language: str
    terms: tuple[Term, ...]


@dataclass(frozen=True)
class Match:
    category: str
    mode: str
    term: str


def load_catalogue(path: Path) -> Catalogue:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read catalogue: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: catalogue must be a mapping")

    language = data.get("language")
    if not isinstance(language, str) or not language:
        raise ConfigError(f"{path}: catalogue must declare a string language")

    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: catalogue must have a non-empty entries list")

    terms: list[Term] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: entries[{index}] must be a mapping")

        category = entry.get("category")
        if not isinstance(category, str) or not category:
            raise ConfigError(
                f"{path}: entries[{index}].category must be a non-empty string "
                f"(got {category!r}; quote reserved YAML words such as 'yes'/'no')"
            )

        mode = entry.get("mode")
        if mode not in _MODES:
            raise ConfigError(f"{path}: entries[{index}].mode must be one of {_MODES}, got {mode!r}")

        raw_terms = entry.get("terms")
        if not isinstance(raw_terms, list) or not raw_terms:
            raise ConfigError(f"{path}: entries[{index}].terms must be a non-empty list")

        for raw_term in raw_terms:
            if not isinstance(raw_term, str) or not raw_term.strip():
                raise ConfigError(f"{path}: entries[{index}] has an empty or non-string term")
            tokens = tuple(normalise(raw_term))
            if not tokens:
                raise ConfigError(f"{path}: term {raw_term!r} normalises to no tokens")
            if mode in ("word", "prefix") and len(tokens) != 1:
                raise ConfigError(
                    f"{path}: term {raw_term!r} normalises to multiple tokens {tokens!r}; "
                    f"use mode 'phrase' for multi-word terms"
                )
            terms.append(Term(category=category, mode=mode, raw=raw_term, tokens=tokens))

    return Catalogue(language=language, terms=tuple(terms))


def _term_matches(term: Term, tokens: list[str]) -> bool:
    if term.mode == "word":
        return term.tokens[0] in tokens
    if term.mode == "prefix":
        prefix = term.tokens[0]
        return any(token.startswith(prefix) for token in tokens)
    length = len(term.tokens)
    span = len(tokens) - length + 1
    return any(tuple(tokens[i:i + length]) == term.tokens for i in range(max(span, 0)))


def find_matches(text: str, catalogue: Catalogue) -> list[Match]:
    tokens = normalise(text)
    if not tokens:
        return []
    return [
        Match(category=term.category, mode=term.mode, term=term.raw)
        for term in catalogue.terms
        if _term_matches(term, tokens)
    ]


def is_affirmative(text: str, catalogue: Catalogue) -> bool:
    if any(symbol in text for symbol in _EXPLICIT_AFFIRMATIVE_SYMBOLS):
        return True
    return bool(find_matches(text, catalogue))


def is_negated(text: str, catalogue: Catalogue) -> bool:
    return bool(find_matches(text, catalogue))


def confirms_dose(text: str, affirmative_catalogue: Catalogue, negative_catalogue: Catalogue) -> bool:
    if is_negated(text, negative_catalogue):
        return False
    return is_affirmative(text, affirmative_catalogue)
