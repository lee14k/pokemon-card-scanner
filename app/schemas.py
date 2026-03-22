"""API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CardMatch(BaseModel):
    id: str
    name: str
    set_name: str | None = None
    number: str | None = None
    rarity: str | None = None
    images: dict[str, str | None] | None = None
    tcgplayer: dict[str, Any] | None = None
    cardmarket: dict[str, Any] | None = None
    match_score: float = Field(..., description="0–100 fuzzy match vs OCR text")


class PriceLookupResponse(BaseModel):
    ocr_text_sample: str | None = None
    query_fragments: list[str] = Field(default_factory=list)
    matches: list[CardMatch]


class CardAnalyzeResponse(BaseModel):
    """OCR / symbol parse only — no PokéWallet call."""

    pokemon_name: str | None = None
    set_id: str | None = None
    set_code: str | None = None
    symbol_match_distance: int | None = None
    collection_number: str | None = Field(
        None, description="Parsed NNN/NNN from the card"
    )
    ocr_text_sample: str | None = None
    suggested_search_queries: list[str] = Field(default_factory=list)
    ocr_fragments: list[str] = Field(default_factory=list)
