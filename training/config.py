"""Paths and constants for the training pipeline. All data lives under
training/data/ (gitignored) except exported models (committed via matcher/)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
REFS_RAW = DATA / "refs_raw"        # hires reference card images per set slug
BACKGROUNDS = DATA / "backgrounds"  # real-photo background crops
RUNS = ROOT / "runs"

EMBED_DIM = 256
# Wide input matching strip geometry (~4:1): square inputs crush strips
# 13.5x horizontally vs 3.3x for reference crops — mismatched texture
# statistics between the towers (v1d experiment).
IMG_W = 384
IMG_H = 96
REF_BOTTOM_FRAC = 0.14   # must match matcher/app.py REF_BOTTOM_FRAC
DEFAULT_SETS = ["sv6", "sv1", "swsh9"]  # phase-1 training sets (~600 cards)
