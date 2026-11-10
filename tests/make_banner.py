"""Build assets/banner.jpg (1200x600) and assets/dashboard.png from a live
Mission Control screenshot: crop clean regions (two columns, each given as
x0 y0 x1 y1) and add a title strip in the app's own dark palette.

    python tests/make_banner.py <screenshot.png> <left box> [<right box>]
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
src = sys.argv[1]
nums = [int(v) for v in sys.argv[2:]]
boxes = [tuple(nums[i:i + 4]) for i in range(0, len(nums), 4)]
shot = Image.open(src).convert("RGB")
BG = shot.getpixel((8, 8))                     # the app's own background colour

# Columns side by side, top-aligned, with the screenshot's own gutter between them.
parts = [shot.crop(b) for b in boxes]
GAP = 16
crop = Image.new("RGB", (sum(p.width for p in parts) + GAP * (len(parts) - 1), max(p.height for p in parts)), BG)
x = 0
for p in parts:
    crop.paste(p, (x, 0))
    x += p.width + GAP
FG, MUTED, ACCENT, RED = (236, 236, 236), (140, 140, 140), (88, 196, 140), (232, 92, 106)
FONTS = Path("C:/Windows/Fonts")


def font(name, size):
    for n in (name, "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(str(FONTS / n), size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---- banner 1200x600 ----
W, H, PAD, TOP = 1200, 600, 36, 150
banner = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(banner)

d.ellipse((PAD, 50, PAD + 14, 64), fill=ACCENT)
d.text((PAD + 26, 30), "Mission Control", font=font("segoeuib.ttf", 46), fill=FG)
d.text((PAD + 26, 92), "Live tool waterfall · parallel subagents · token & cost meters · burn and loop alerts",
       font=font("segoeui.ttf", 21), fill=MUTED)
tag = "for Hermes Desktop"
tf = font("segoeuisb.ttf", 18)
tw = d.textlength(tag, font=tf)
d.rounded_rectangle((W - PAD - tw - 28, 40, W - PAD, 74), radius=17, outline=(70, 70, 70), width=1)
d.text((W - PAD - tw - 14, 44), tag, font=tf, fill=FG)

avail_w, avail_h = W - 2 * PAD, H - TOP - PAD
scale = min(avail_w / crop.width, avail_h / crop.height)
shown = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
shown = shown.filter(ImageFilter.UnsharpMask(radius=1, percent=60, threshold=2))
x = (W - shown.width) // 2
banner.paste(shown, (x, TOP))
# soft fade at the bottom edge so the crop doesn't end on a hard cut
fade = Image.new("L", (W, 40))
for i in range(40):
    ImageDraw.Draw(fade).line((0, i, W, i), fill=int(255 * i / 39))
banner.paste(Image.new("RGB", (W, 40), BG), (0, H - 40), fade)

(ROOT / "assets").mkdir(exist_ok=True)
banner.save(ROOT / "assets" / "banner.jpg", quality=90, optimize=True, progressive=True)

# ---- dashboard.png: the clean crop itself, with a little breathing room ----
dash = Image.new("RGB", (crop.width + 48, crop.height + 48), BG)
dash.paste(crop, (24, 24))
dash.save(ROOT / "assets" / "dashboard.png", optimize=True)
print("banner.jpg", banner.size, "| dashboard.png", dash.size, "| crop scale", round(scale, 3))
