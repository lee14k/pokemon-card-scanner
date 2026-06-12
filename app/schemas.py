"""API response models for the pack scanner."""

from __future__ import annotations

from pydantic import BaseModel


class PackCard(BaseModel):
    row_index: int = -1  # -1 for /cards/lookup results (not tied to a photo row)
    card_number: str | None = None   # as printed: "123/198", "TG12/TG30", "SWSH123"
    set_id: str | None = None        # PokéWallet numeric set id (string)
    set_code: str | None = None      # e.g. "SVI"
    set_name: str | None = None
    name: str | None = None
    rarity: str | None = None
    image_url: str | None = None
    match_id: str | None = None      # PokéWallet card id
    confidence: float = 0.0
    low_confidence_reason: str | None = None
    # one of: unreadable_strip | number_ambiguous | set_ambiguous | no_db_match


class CodeCardResult(BaseModel):
    code: str | None = None
    confidence: float = 0.0
    format_ok: bool = False


class PackScanResponse(BaseModel):
    cards: list[PackCard]
    code_card: CodeCardResult
    pack_confidence: float
    segmentation_warning: str | None = None


class SetInfo(BaseModel):
    set_id: str
    set_code: str | None = None
    set_name: str
    denominators: list[str]
    era: str  # "swsh" | "sv"


class CardLookupResponse(BaseModel):
    found: bool
    card: PackCard | None = None
