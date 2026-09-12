from __future__ import annotations

import re

REDACTED_DOSAGE = "[redacted dosage]"
MAX_EXCERPT_LENGTH = 280
EXCERPT_TRUNCATION_MARKER = "…"

_UNITS = {
    "mg", "mgs", "mg.", "mcg", "g", "gram", "grams",
    "ml", "mls", "l", "milligram", "milligrams", "milliliter", "milliliters",
    "мг", "мкг", "г", "мл", "л",
    "tablet", "tablets", "pill", "pills", "capsule", "capsules", "dose", "doses",
    "таблетка", "таблетки", "таблетку", "таблеток",
    "капсула", "капсули", "капсул",
    "крапля", "краплі", "крапель",
}

_DOSAGE_RE = re.compile(r"(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>[^\W\d_]+)", re.UNICODE)


def _find_dosage_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _DOSAGE_RE.finditer(text):
        if match.group("unit").casefold() in _UNITS:
            spans.append(match.span())
    return spans


def contains_dosage_notation(text: str) -> bool:
    return bool(_find_dosage_spans(text))


def redact_dosage(text: str) -> str:
    spans = _find_dosage_spans(text)
    if not spans:
        return text
    pieces = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        pieces.append(REDACTED_DOSAGE)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def bound_excerpt(text: str, max_length: int = MAX_EXCERPT_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    keep = max(max_length - len(EXCERPT_TRUNCATION_MARKER), 0)
    return text[:keep] + EXCERPT_TRUNCATION_MARKER


def redact_and_bound(text: str, max_length: int = MAX_EXCERPT_LENGTH) -> str:
    return bound_excerpt(redact_dosage(text), max_length)
