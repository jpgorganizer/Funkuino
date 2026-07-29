#!/usr/bin/env python3
"""Render Funkuino.icns from the Studio wordmark's wave motif.

There is no logo file to convert: the mark in the web UI is three CSS bars
(``.wave i`` in style.css). This redraws them at icon sizes and hands the set to
``iconutil``. Colours mirror the UI's accent on its dark surface.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "Funkuino.icns"
# Taken from style.css (converted from oklch): --accent on the dark --surface.
BG_TOP = (46, 38, 31)
BG_BOTTOM = (33, 27, 22)
ACCENT = (190, 86, 33)
# Bar heights as a fraction of the icon, mirroring the wave's short-tall-short.
BARS = (0.34, 0.62, 0.44)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)

    # Rounded-square background with a soft vertical gradient (macOS-ish).
    for y in range(size):
        t = y / max(size - 1, 1)
        draw.line([(0, y), (size, y)],
                  fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=round(size * 0.225), fill=255)
    img.putalpha(mask)

    bar_w = size * 0.116
    gap = size * 0.072
    total = len(BARS) * bar_w + (len(BARS) - 1) * gap
    x = (size - total) / 2
    for fraction in BARS:
        height = size * fraction
        y0 = (size - height) / 2
        ImageDraw.Draw(img).rounded_rectangle(
            [x, y0, x + bar_w, y0 + height], radius=bar_w / 2, fill=ACCENT)
        x += bar_w + gap
    return img


def main() -> int:
    if shutil.which("iconutil") is None:
        print("iconutil not found (macOS only).", file=sys.stderr)
        return 1
    iconset = OUT.with_suffix(".iconset")
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    for base in (16, 32, 128, 256, 512):
        render(base).save(iconset / f"icon_{base}x{base}.png")
        render(base * 2).save(iconset / f"icon_{base}x{base}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    shutil.rmtree(iconset)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
