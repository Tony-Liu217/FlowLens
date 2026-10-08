"""Non-destructive background suppression for OCR, never document restoration.

Derived views may erase coloured/faint legitimate text. Keep the original and
compare observations; never treat these views as authoritative source images.
No inpainting, character substitution, decimal/sign guessing or balance repair.
"""
from __future__ import annotations

import cv2
import numpy as np

VERSION = "watermark-0.1"
MAX_PIXELS = 20_000_000


def recognition_views(rgb):
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("EXPECTED_UINT8_RGB")
    if not rgb.size or rgb.shape[0] * rgb.shape[1] > MAX_PIXELS:
        raise ValueError("IMAGE_SIZE_LIMIT")
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Max channel suppresses saturated red/blue marks, but can also suppress
    # coloured amounts. It is an auxiliary view, not a replacement for RGB.
    neutral = rgb.max(axis=2)
    views, metadata = {}, {}
    for name, channel in [("dark_gray", gray), ("dark_neutral", neutral)]:
        otsu, _ = cv2.threshold(channel, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cutoff = min(180, int(otsu))
        ink = channel <= cutoff
        fraction = float(ink.mean())
        meta = {"method": "otsu_capped_dark_ink", "threshold": cutoff,
                "otsu_threshold": float(otsu), "ink_fraction": fraction}
        # A global dark-ink filter is unsuitable for low-contrast pages.
        # Reject near-empty or implausibly dense derived views, preserve RGB.
        if otsu > 210 or fraction < .001 or fraction > .35:
            meta["skipped"] = "UNSUITABLE_CONTRAST"
        else:
            binary = np.where(ink, 0, 255).astype(np.uint8)
            views[name] = np.repeat(binary[:, :, None], 3, axis=2)
        metadata[name] = meta
    return views, metadata
