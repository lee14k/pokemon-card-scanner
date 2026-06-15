"""Render synthetic staircase / code-card photos with known ground truth."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARD_W, CARD_H = 750, 1050
EXPOSED = 120  # px of each card's bottom strip left visible
MARGIN = 80


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def make_card(number_text: str, set_code: str = "") -> Image.Image:
    # No decorative inner frame: real card faces are busy art, not clean geometric
    # lines. A high-contrast inner rectangle would create spurious full-width
    # horizontal Hough edges on the fully-visible top card, over-segmenting the
    # ungrided path with phantoms real photos don't have. Only the card's bottom
    # border (the edge segmentation keys on) and the number label are drawn.
    im = Image.new("RGB", (CARD_W, CARD_H), (250, 245, 230))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=(40, 40, 40), width=6)
    label = f"{set_code} {number_text}".strip()
    d.text((40, CARD_H - 70), label, fill=(20, 20, 20), font=_font(38))
    return im


def make_staircase(
    entries: list[tuple[str, str]],
    out_path: Path,
    *,
    blur_rows: set[int] = frozenset(),
) -> dict:
    """
    entries: [(number_text, set_code)] top-to-bottom; row_index 0 = topmost edge.
    Painted back-to-front so each card's bottom EXPOSED px stay visible.
    Returns capture_meta dict matching the API contract.
    """
    n = len(entries)
    w = CARD_W + 2 * MARGIN
    h = MARGIN + CARD_H + EXPOSED * (n - 1) + MARGIN
    sheet = Image.new("RGB", (w, h), (90, 110, 90))
    for i in range(n - 1, -1, -1):
        num, code = entries[i]
        sheet.paste(make_card(num, code), (MARGIN, MARGIN + i * EXPOSED))
    guide_positions = [MARGIN + i * EXPOSED + CARD_H for i in range(n)]
    for r in blur_rows:
        y1 = guide_positions[r]
        y0 = max(0, y1 - EXPOSED)
        box = (MARGIN, y0, MARGIN + CARD_W, y1)
        sheet.paste(sheet.crop(box).filter(ImageFilter.GaussianBlur(8)), box)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "JPEG", quality=85)
    return {
        "guide_positions": guide_positions,
        "image_dims": [w, h],
        "declared_count": n,
    }


def make_code_card(code_text: str, out_path: Path) -> None:
    im = Image.new("RGB", (1000, 700), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 999, 699], outline=(0, 0, 0), width=8)
    d.text((90, 300), code_text, fill=(0, 0, 0), font=_font(64))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, "JPEG", quality=90)
