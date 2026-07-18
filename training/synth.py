"""Synthesize staircase-scene photos with per-card ground truth bands.
Deterministic per seed. Cards: hires reference images; look: degradation stack
approximating real user photos (see spec)."""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from training.config import BACKGROUNDS, REFS_RAW


@dataclass
class SceneTruth:
    card_keys: list[str]        # front (fully visible) first
    band_centers: list[float]   # y of each card's visible band center, post-warp
    band_height: float


def _load_refs(slug: str) -> list[Path]:
    return sorted((REFS_RAW / slug).glob("*.png"))


def _rand_background(rng: random.Random, w: int, h: int) -> np.ndarray:
    bgs = sorted(BACKGROUNDS.glob("*.jpg"))
    if bgs and rng.random() < 0.7:
        bg = cv2.imread(str(rng.choice(bgs)))
        return cv2.resize(bg, (w, h))
    base = np.full((h, w, 3), rng.randint(30, 220), np.uint8)
    noise = rng.randint(5, 25)
    return cv2.add(base, cv2.randn(np.zeros((h, w, 3), np.int16), 0, noise).astype(np.uint8))


def _degrade(img: np.ndarray, rng: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    # defocus / motion blur
    if rng.random() < 0.8:
        k = rng.choice([3, 5, 7, 9])
        img = cv2.GaussianBlur(img, (k, k), 0)
    if rng.random() < 0.3:
        k = rng.choice([5, 9, 13])
        kern = np.zeros((k, k), np.float32); kern[k // 2, :] = 1.0 / k
        ang = rng.uniform(0, 180)
        m = cv2.getRotationMatrix2D((k / 2, k / 2), ang, 1.0)
        img = cv2.filter2D(img, -1, cv2.warpAffine(kern, m, (k, k)))
    # haze (contrast lift)
    if rng.random() < 0.6:
        a = rng.uniform(0.05, 0.35)
        img = cv2.addWeighted(img, 1 - a, np.full_like(img, 230), a, 0)
    # glare streaks/blobs
    for _ in range(rng.randint(0, 3)):
        overlay = np.zeros_like(img)
        cx, cy = rng.randint(0, w), rng.randint(0, h)
        ax, ay = rng.randint(w // 8, w // 2), rng.randint(10, h // 6)
        cv2.ellipse(overlay, (cx, cy), (ax, ay), rng.uniform(0, 180), 0, 360,
                    (255, 255, 255), -1)
        overlay = cv2.GaussianBlur(overlay, (0, 0), rng.uniform(15, 60))
        img = cv2.add(img, (overlay * rng.uniform(0.15, 0.5)).astype(np.uint8))
    # color cast + vignette
    if rng.random() < 0.7:
        cast = np.array([rng.uniform(0.85, 1.15) for _ in range(3)])
        img = np.clip(img.astype(np.float32) * cast, 0, 255).astype(np.uint8)
    if rng.random() < 0.5:
        ys, xs = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xs - w / 2) / (w / 2)) ** 2 + ((ys - h / 2) / (h / 2)) ** 2)
        vig = 1 - rng.uniform(0.1, 0.35) * np.clip(d - 0.5, 0, 1)
        img = np.clip(img.astype(np.float32) * vig[..., None], 0, 255).astype(np.uint8)
    # sensor noise
    if rng.random() < 0.7:
        img = cv2.add(img, cv2.randn(np.zeros_like(img, np.int16), 0,
                                     rng.randint(2, 10)).astype(np.uint8))
    # resolution chain + jpeg
    if rng.random() < 0.6:
        s = rng.uniform(0.45, 0.85)
        img = cv2.resize(cv2.resize(img, None, fx=s, fy=s), (w, h))
    q = rng.randint(50, 95)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.imdecode(enc, cv2.IMREAD_COLOR)


def _finger(img: np.ndarray, rng: random.Random) -> None:
    h, w = img.shape[:2]
    cx, cy = rng.randint(w // 3, w - 1), rng.randint(0, h // 2)
    ax, ay = rng.randint(w // 10, w // 4), rng.randint(h // 8, h // 3)
    tone = (rng.randint(120, 190), rng.randint(140, 200), rng.randint(180, 230))
    overlay = img.copy()
    cv2.ellipse(overlay, (cx, cy), (ax, ay), rng.uniform(-30, 30), 0, 360, tone, -1)
    a = rng.uniform(0.85, 1.0)
    cv2.addWeighted(overlay, a, img, 1 - a, 0, dst=img)


def synth_scene(slug: str, seed: int, k: int | None = None
                ) -> tuple[np.ndarray, SceneTruth]:
    rng = random.Random(seed)
    refs = _load_refs(slug)
    k = k or rng.choice([1, 2, 3, 5, 8, 10, 11, 12])
    picks = rng.sample(refs, min(k, len(refs)))
    card_keys = [p.stem for p in picks]

    card_w = rng.randint(900, 1600)
    card0 = cv2.imread(str(picks[0]))
    ch = int(card0.shape[0] * card_w / card0.shape[1])
    gap = int(ch * rng.uniform(0.07, 0.14))

    W = int(card_w * rng.uniform(1.15, 1.6))
    H = int(ch + gap * (len(picks) - 1) + ch * rng.uniform(0.2, 0.5))
    canvas = _rand_background(rng, W, H)
    x0 = (W - card_w) // 2 + rng.randint(-card_w // 10, card_w // 10)
    y0 = int(ch * rng.uniform(0.05, 0.2))

    band_centers = []
    # draw back-to-front: card i sits i*gap lower; front card (index 0) on top
    for i in reversed(range(len(picks))):
        card = cv2.resize(cv2.imread(str(picks[i])), (card_w, ch))
        y = y0 + i * gap
        ang = rng.uniform(-2.5, 2.5)
        m = cv2.getRotationMatrix2D((card_w / 2, ch / 2), ang, 1.0)
        card = cv2.warpAffine(card, m, (card_w, ch), borderMode=cv2.BORDER_REPLICATE)
        ys, ye = max(0, y), min(H, y + ch)
        xs, xe = max(0, x0), min(W, x0 + card_w)
        canvas[ys:ye, xs:xe] = card[ys - y:ye - y, xs - x0:xe - x0]
    for i in range(len(picks)):
        bottom = y0 + i * gap + ch
        band_centers.append(bottom - gap / 2)

    if rng.random() < 0.5:
        _finger(canvas, rng)

    # global perspective/rotation
    ang = rng.uniform(-8, 8)
    m = cv2.getRotationMatrix2D((W / 2, H / 2), ang, rng.uniform(0.92, 1.0))
    canvas = cv2.warpAffine(canvas, m, (W, H), borderMode=cv2.BORDER_REPLICATE)
    pts = np.array([[[W / 2, y] for y in band_centers]], np.float32)
    band_centers = [float(p[1]) for p in cv2.transform(pts, m)[0]]

    canvas = _degrade(canvas, rng)
    return canvas, SceneTruth(card_keys, band_centers, float(gap))
