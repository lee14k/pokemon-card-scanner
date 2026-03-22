"""Structured fields extracted from a card scan (name, number, optional set from symbol)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ASCII /, fullwidth ／, fraction slash ⁄, division slash ∕
_COLLECTION_NUM_RE = re.compile(r"(\d{1,4})\s*[/／\u2044\u2215]\s*(\d{1,4})")


def pick_collection_number(*text_blobs: str) -> str | None:
    """
    Parse NNN/NNN collection number from OCR text.
    Pass blobs in order: earlier = lower priority, later = higher (e.g. full card, top band, bottom strip).
    """
    best_str: str | None = None
    best_key = (-1, -1)

    for bi, blob in enumerate(text_blobs):
        if not blob:
            continue
        for m in _COLLECTION_NUM_RE.finditer(blob):
            try:
                a, b = int(m.group(1)), int(m.group(2))
            except ValueError:
                continue
            if b < 1 or b > 450 or a < 0 or a > b + 80:
                continue
            if b >= 1900:
                continue
            key = (bi, m.start())
            if key > best_key:
                best_key = key
                best_str = f"{m.group(1)}/{m.group(2)}"
    return best_str


@dataclass
class CardSignals:
    """OCR + optional set-symbol match used for PokéWallet queries."""

    ocr_fragments: list[str] = field(default_factory=list)
    card_number: str | None = None
    primary_name_guess: str | None = None
    bottom_raw_ocr: str = ""
    symbol_raw_note: str = ""
    set_id_from_symbol: str | None = None
    set_code_from_symbol: str | None = None
    symbol_hash_distance: int | None = None

    @staticmethod
    def empty() -> CardSignals:
        return CardSignals()
