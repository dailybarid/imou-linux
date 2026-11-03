#!/usr/bin/env python3
"""Generate the project artwork: logo, README banner and social preview.

Everything is drawn programmatically at 2x and downsampled, so the PNGs stay
sharp and can be regenerated at any time. The SVG logo is hand-written
(logo.svg) to keep a true vector source for the icon.
"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")
os.makedirs(OUT, exist_ok=True)

BG = (15, 17, 22)
BG2 = (22, 26, 33)
FG = (231, 234, 240)
MUTED = (138, 147, 166)
ACCENT = (77, 141, 255)
GRAD_TOP = (91, 155, 255)
GRAD_BOT = (54, 108, 214)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def gradient(size):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        g.putpixel((0, y), tuple(int(GRAD_TOP[i] + (GRAD_BOT[i] - GRAD_TOP[i]) * t)
                                 for i in range(3)))
    return g.resize((size, size), Image.BILINEAR)


def camera_mark(size):
    """The logo glyph as RGBA, transparent outside the rounded square."""
    S = size * 4
    k = S / 512.0
    bg = gradient(S).convert("RGBA")

    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([72 * k, 141 * k, 440 * k, 392 * k], radius=45 * k, fill=255)
    d.rounded_rectangle([150 * k, 110 * k, 270 * k, 160 * k], radius=18 * k, fill=255)
    d.ellipse([186 * k, 206 * k, 326 * k, 346 * k], fill=255)
    d.ellipse([98 * k, 173 * k, 128 * k, 203 * k], fill=255)

    hole = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hole).ellipse([213 * k, 233 * k, 299 * k, 319 * k], fill=255)
    m = Image.composite(Image.new("L", (S, S), 0), m, hole)

    ImageDraw.Draw(m).ellipse([229 * k, 246 * k, 263 * k, 280 * k], fill=255)

    bg.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0), m)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=112 * k,
                                           fill=255)
    bg.putalpha(mask)
    return bg.resize((size, size), Image.LANCZOS)


def glow(img, cx, cy, r, color, alpha=38):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(r, 0, -max(1, r // 30)):
        a = int(alpha * (1 - i / r) ** 2)
        d.ellipse([cx - i, cy - i, cx + i, cy + i], fill=color + (a,))
    return Image.alpha_composite(img, layer)


def banner():
    W, H = 1280, 360
    img = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(img)
    for x in range(W):                       # faint vertical accent edge
        a = int(16 * (1 - abs(x - W / 2) / (W / 2)))
        d.line([(x, 0), (x, H)], fill=BG2 + (a,))
    img = glow(img, 190, H // 2, 260, ACCENT, 30)

    mark = camera_mark(200)
    img.alpha_composite(mark, (72, (H - 200) // 2))

    tx = 320
    d = ImageDraw.Draw(img)
    d.text((tx, 92), "Imou on Linux", font=font(FONT_BOLD, 62), fill=FG + (255,))
    d.text((tx, 172), "RTSP · ONVIF · DVRIP control for Imou & Dahua cameras",
           font=font(FONT_REG, 25), fill=MUTED + (255,))
    d.text((tx, 218), "No vendor app.  No cloud.  No Windows required.",
           font=font(FONT_BOLD, 25), fill=ACCENT + (255,))
    d.line([(tx, 268), (tx + 480, 268)], fill=(39, 45, 56, 255), width=2)
    d.text((tx, 286), "live view · image & night tuning · motion · SD card · local recording",
           font=font(FONT_REG, 18), fill=MUTED + (255,))
    return img


def social():
    W, H = 1280, 640
    img = Image.new("RGBA", (W, H), BG + (255,))
    img = glow(img, W // 2, 250, 420, ACCENT, 34)
    mark = camera_mark(190)
    img.alpha_composite(mark, ((W - 190) // 2, 96))

    d = ImageDraw.Draw(img)
    f1, f2, f3 = font(FONT_BOLD, 62), font(FONT_REG, 26), font(FONT_BOLD, 24)
    t = "Imou on Linux"
    d.text(((W - d.textlength(t, f1)) / 2, 318), t, font=f1, fill=FG + (255,))
    s = "Local RTSP · ONVIF · DVRIP control for Imou & Dahua cameras"
    d.text(((W - d.textlength(s, f2)) / 2, 402), s, font=f2, fill=MUTED + (255,))
    t3 = "No vendor app.  No cloud.  No Windows required."
    d.text(((W - d.textlength(t3, f3)) / 2, 458), t3, font=f3, fill=ACCENT + (255,))
    return img


def main():
    logo = camera_mark(512)
    logo.save(os.path.join(OUT, "logo.png"))
    logo.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "logo-256.png"))
    banner().convert("RGB").save(os.path.join(OUT, "banner.png"))
    social().convert("RGB").save(os.path.join(OUT, "social-preview.png"))
    for n in ("logo.png", "banner.png", "social-preview.png"):
        p = os.path.join(OUT, n)
        print(f"{n:20s} {os.path.getsize(p) / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
