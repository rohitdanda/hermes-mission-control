"""Fit an image to the catalog banner: centre-crop to exactly 2:1, resize to 1200x600, save JPEG.

    python tests/fit_banner.py <source image> [assets/banner.jpg]
"""
import sys
from pathlib import Path

from PIL import Image

src = Path(sys.argv[1])
out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "assets" / "banner.jpg"
im = Image.open(src).convert("RGB")
w, h = im.size
if w >= 2 * h:
    cw, ch = 2 * h, h
else:
    cw, ch = w, w // 2
left, top = (w - cw) // 2, (h - ch) // 2
im = im.crop((left, top, left + cw, top + ch)).resize((1200, 600), Image.LANCZOS)
im.save(out, "JPEG", quality=90, optimize=True, progressive=True)
print(f"{src.name} {w}x{h} -> crop {cw}x{ch} at ({left},{top}) -> {out} 1200x600, {out.stat().st_size} bytes")
