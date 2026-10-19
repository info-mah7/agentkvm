"""Per-frame cursor localization from pixels alone - no OS hooks on the target.

Two mechanisms, cheapest first (the order the handoff calls for):

1. **Frame-diff against a static desktop.** The background is estimated as the
   per-pixel median of several frames taken while the cursor is jiggled, which
   removes the moving cursor. Each later frame is differenced against it; the
   cursor is the cursor-sized blob that remains.
2. **Template match.** Once located, a patch around the cursor is kept and
   matched with normalized cross-correlation - robust when the desktop under
   the cursor is not perfectly static.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Detection:
    x: float  # blob centroid, pixels
    y: float
    confidence: float  # 0..1
    area: float
    bbox: tuple[int, int, int, int]  # x, y, w, h


def _gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return img


class CursorDetector:
    def __init__(
        self,
        *,
        diff_thresh: int = 28,
        min_area: int = 30,
        max_area: int = 1600,
        template_size: int = 40,
    ) -> None:
        self.diff_thresh = diff_thresh
        self.min_area = min_area
        self.max_area = max_area
        self.template_size = template_size
        self._bg: np.ndarray | None = None  # grayscale float32
        self._template: np.ndarray | None = None
        self._template_hotspot = (0, 0)

    # ------------------------------------------------------------- background
    def estimate_background(self, images: list[np.ndarray]) -> np.ndarray:
        """Median-stack cursor-free background from frames with a moving cursor."""
        if not images:
            raise ValueError("need at least one frame")
        stack = np.stack([_gray(im).astype(np.float32) for im in images], axis=0)
        self._bg = np.median(stack, axis=0).astype(np.float32)
        return self._bg

    def set_background(self, image: np.ndarray) -> None:
        self._bg = _gray(image).astype(np.float32)

    @property
    def has_background(self) -> bool:
        return self._bg is not None

    # ------------------------------------------------------------- detection
    def locate(self, image: np.ndarray, near: tuple[float, float] | None = None) -> Detection | None:
        """Locate the cursor by differencing against the background.

        ``near`` disambiguates when more than one region changed: the blob
        closest to that expected point wins.
        """
        if self._bg is None:
            raise RuntimeError("no background set; call estimate_background() first")
        gray = _gray(image).astype(np.float32)
        diff = cv2.absdiff(gray, self._bg).astype(np.uint8)
        _, mask = cv2.threshold(diff, self.diff_thresh, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: Detection | None = None
        best_score = -1.0
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area or area > self.max_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            m = cv2.moments(c)
            if m["m00"] == 0:
                continue
            cx = m["m10"] / m["m00"]
            cy = m["m01"] / m["m00"]
            # score: prefer cursor-sized, compact blobs; optionally near expected
            fill = area / max(w * h, 1)
            size_fit = 1.0 - min(abs(area - 400) / 1200.0, 1.0)
            score = 0.5 * size_fit + 0.5 * fill
            if near is not None:
                dist = np.hypot(cx - near[0], cy - near[1])
                score += max(0.0, 1.0 - dist / 200.0)
            if score > best_score:
                best_score = score
                best = Detection(cx, cy, float(np.clip(score if near is None else score / 2, 0, 1)),
                                 float(area), (x, y, w, h))
        return best

    # ------------------------------------------------------------- template
    def capture_template(self, image: np.ndarray, center: tuple[float, float]) -> None:
        gray = _gray(image)
        s = self.template_size
        cx, cy = int(center[0]), int(center[1])
        x0 = max(0, cx - s // 2)
        y0 = max(0, cy - s // 2)
        patch = gray[y0 : y0 + s, x0 : x0 + s]
        if patch.size == 0:
            return
        self._template = patch.copy()
        self._template_hotspot = (cx - x0, cy - y0)

    def locate_template(self, image: np.ndarray) -> Detection | None:
        if self._template is None:
            return None
        gray = _gray(image)
        res = cv2.matchTemplate(gray, self._template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        th, tw = self._template.shape[:2]
        x = max_loc[0] + tw / 2.0
        y = max_loc[1] + th / 2.0
        return Detection(x, y, float(max(0.0, max_val)), float(tw * th),
                         (max_loc[0], max_loc[1], tw, th))
