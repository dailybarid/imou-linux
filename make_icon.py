#!/usr/bin/env python3
"""Generate the Imou Camera Control app icon (all hicolor sizes) with PIL.

Design: accent-blue rounded square, white camera silhouette, hollow lens so the
background gradient shows through, plus a glint and a status LED. Drawn at 4x and
downsampled for clean anti-aliased edges.
"""
import os
from PIL import Image, ImageDraw

S = 2048          # supersampled canvas (512 * 4)
BASE = 512
R = S // BASE     # scale factor

GRAD_TOP = (91, 155, 255)      # #5b9bff
GRAD_BOT = (54, 108, 214)      # #366cd6
WHITE = (255, 255, 255, 255)

SIZES = [16, 24, 32, 48, 64, 96, 128, 256, 512]
OUT = os.path.expanduser("~/.local/share/icons/hicolor")


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def gradient(size):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        g.putpixel((0, y), tuple(int(GRAD_TOP[i] + (GRAD_BOT[i] - GRAD_TOP[i]) * t)
                                 for i in range(3)))
    return g.resize((size, size), Image.BILINEAR)


def build():
    bg = gradient(S).convert("RGBA")

    # --- white camera silhouette as a mask ---
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)

    # body
    d.rounded_rectangle([286, 566, 1762, 1570], radius=180, fill=255)
    # top bump (viewfinder)
    d.rounded_rectangle([600, 470, 1080, 620], radius=70, fill=255)
    # lens: outer circle (will be hollowed)
    d.ellipse([742, 822, 1306, 1386], fill=255)
    # LED
    d.ellipse([392, 690, 512, 810], fill=255)

    # hollow the lens so the gradient shows through -> ring look
    hole = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hole).ellipse([852, 932, 1196, 1276], fill=255)
    m = Image.composite(Image.new("L", (S, S), 0), m, hole)

    # restore the glint inside the lens
    ImageDraw.Draw(m).ellipse([916, 986, 1052, 1122], fill=255)

    white = Image.new("RGBA", (S, S), WHITE)
    bg.paste(white, (0, 0), m)

    # --- rounded outer shape ---
    bg.putalpha(rounded_mask(S, 112 * R))
    return bg


def main():
    icon = build()
    written = []
    for sz in SIZES:
        d = os.path.join(OUT, f"{sz}x{sz}", "apps")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "imou-camera.png")
        icon.resize((sz, sz), Image.LANCZOS).save(p, "PNG")
        written.append(p)
    # reference copy for the repo / window icon
    here = os.path.dirname(os.path.abspath(__file__))
    icon.resize((512, 512), Image.LANCZOS).save(os.path.join(here, "icon.png"))
    icon.resize((128, 128), Image.LANCZOS).save(os.path.join(here, "icon-128.png"))
    print(f"wrote {len(written)} sizes + icon.png")
    for p in written:
        print(" ", p)


if __name__ == "__main__":
    main()
