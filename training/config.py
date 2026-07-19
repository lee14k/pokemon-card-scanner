"""Paths and constants for the training pipeline. All data lives under
training/data/ (gitignored) except exported models (committed via matcher/)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
REFS_RAW = DATA / "refs_raw"        # hires reference card images per set slug
BACKGROUNDS = DATA / "backgrounds"  # real-photo background crops
UPLOADS = DATA / "uploads"          # fetched real training strips (fetch_uploads.py)
RUNS = ROOT / "runs"

EMBED_DIM = 256
# Wide input matching strip geometry (~4:1): square inputs crush strips
# 13.5x horizontally vs 3.3x for reference crops — mismatched texture
# statistics between the towers (v1d experiment).
IMG_W = 384
IMG_H = 96
REF_BOTTOM_FRAC = 0.14   # must match matcher/app.py REF_BOTTOM_FRAC
DEFAULT_SETS = ["sv6", "sv1", "swsh9"]  # phase-1 training sets (~600 cards)

# Uploaded labels use TCGdex set ids (e.g. "sv06"); reference crops (REFS_RAW,
# dataset refs/) use pokemontcg.io slugs (e.g. "sv6"). The mapping is mechanical
# for numbered sets but genuinely inconsistent for subsets across eras
# (Champion's Path swsh35 vs Crown Zenith swsh12pt5), so those are explicit.
_SLUG_OVERRIDES = {
    "swsh3.5": "swsh35", "swsh4.5": "swsh45", "swsh10.5": "swsh10tg",
    "swsh12.5": "swsh12pt5", "swsh9.5tg": "swsh9tg", "swsh10.5tg": "swsh10tg",
    "swsh11.5tg": "swsh11tg", "swsh12.5tg": "swsh12tg", "swsh12.5gg": "swsh12pt5gg",
    "swsh4.5sv": "swsh45sv", "cel25c": "cel25c",
}


def tcgdex_to_ptcgio(slug: str) -> str | None:
    """Map a TCGdex set id ('sv06') to the pokemontcg.io slug refs use ('sv6').
    Mechanical for numbered sets (strip zero-padding, '.5'->'pt5'); subsets that
    break the rule are in _SLUG_OVERRIDES. Returns None if unmappable."""
    if slug in _SLUG_OVERRIDES:
        return _SLUG_OVERRIDES[slug]
    m = re.match(r"^([a-z]+)(\d+)(?:\.(\d+))?([a-z]*)$", slug)
    if not m:
        return None
    prefix, major, minor, suffix = m.groups()
    out = f"{prefix}{int(major)}"
    if minor:
        out += f"pt{minor}"
    return out + suffix


def tcgdex_card_key_to_ref(card_key: str) -> str | None:
    """'sv06-045' -> 'sv6-45' (dataset refs/ naming). None if unmappable."""
    if "-" not in card_key:
        return None
    set_part, num = card_key.rsplit("-", 1)
    slug = tcgdex_to_ptcgio(set_part)
    if slug is None:
        return None
    num_norm = num if re.match(r"^[A-Za-z]", num) else (num.lstrip("0") or "0")
    return f"{slug}-{num_norm}"
