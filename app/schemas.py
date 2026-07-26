"""API response models for the pack scanner."""

from __future__ import annotations

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer


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
    needs_review: bool = False  # true when the card is uncertain (frontend highlights it)
    price_usd_low: float | None = None
    price_usd_high: float | None = None
    # Background-VLM lifecycle, named exactly like live_session's per-card states:
    # "pending_vlm" (a follow-up task is still identifying this row — poll
    # /scan/{pack,binder}/{scan_id}), "ok", "vlm_failed". None means no follow-up
    # applies to this card, which is every response the VLM never touched.
    state: str | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_state(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Omit ``state`` from the serialized card when it is None.

        ``state`` is meaningful only while a background VLM follow-up exists, so a
        plain optional field would have added ``"state": null`` to every card of
        every response that has nothing to do with the VLM (/cards/lookup, every
        VLM-disabled scan, the binder gate's fixtures). Dropping the null keeps
        those payloads byte-for-byte what they were before the field existed —
        which is the contract the binder gate and old clients are held to."""
        dumped = handler(self)
        if dumped.get("state") is None:
            dumped.pop("state", None)
        return dumped


class CodeCardResult(BaseModel):
    code: str | None = None
    confidence: float = 0.0
    format_ok: bool = False


class PackScanResponse(BaseModel):
    cards: list[PackCard]
    code_card: CodeCardResult
    pack_confidence: float
    segmentation_warning: str | None = None
    # Follow-up handle: set only when a background VLM pass is still running for
    # this scan, in which case the flagged cards carry state="pending_vlm" and
    # GET /scan/pack/{scan_id} serves their patched identities. Old clients that
    # ignore it just keep the flagged Phase-1 cards.
    scan_id: str | None = None


class SetInfo(BaseModel):
    set_id: str
    set_code: str | None = None
    set_name: str
    denominators: list[str]
    era: str  # "swsh" | "sv"


class CardLookupResponse(BaseModel):
    found: bool
    card: PackCard | None = None
