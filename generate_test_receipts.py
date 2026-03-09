"""
Generate multiple realistic handwritten receipt images for OCR testing.

Each image simulates different handwriting styles:
  1. Neat handwriting — clean, slightly angled
  2. Messy/rushed handwriting — more jitter, bigger angle, uneven spacing
  3. Faded ink — lighter color, simulating old/faded pen
  4. Mixed case & sloppy — random caps, poor alignment
  5. Dense receipt — many items, tight spacing
"""

import random
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUTPUT_DIR = Path("test_images")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Fonts ─────────────────────────────────────────────────────────────────────
# Try multiple handwriting-like fonts, fallback to default
def get_font(size: int):
    for name in ["comic.ttf", "comicbd.ttf", "segoesc.ttf", "calibri.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _jitter(x, y, amount=2):
    """Add random pixel jitter to simulate shaky handwriting."""
    return x + random.randint(-amount, amount), y + random.randint(-amount, amount)


def _draw_wavy_line(draw, y, width, color, thickness=1):
    """Draw a wavy underline to simulate ruled paper."""
    pts = []
    for x in range(0, width, 4):
        pts.append((x, y + random.randint(-1, 1)))
    if len(pts) > 1:
        draw.line(pts, fill=color, width=thickness)


def _add_paper_texture(img, intensity=8):
    """Add subtle noise to simulate paper grain."""
    import struct
    pixels = img.load()
    w, h = img.size
    for _ in range(w * h // 6):
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        r, g, b = pixels[x, y]
        d = random.randint(-intensity, intensity)
        pixels[x, y] = (
            max(0, min(255, r + d)),
            max(0, min(255, g + d)),
            max(0, min(255, b + d)),
        )


def _add_smudge(img, count=3):
    """Add subtle smudge marks."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for _ in range(count):
        cx, cy = random.randint(50, w - 50), random.randint(50, h - 50)
        r = random.randint(8, 20)
        color = (
            random.randint(200, 230),
            random.randint(195, 225),
            random.randint(185, 215),
        )
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def _add_coffee_stain(img):
    """Add a faint circular coffee-ring stain."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    cx = random.randint(w // 4, 3 * w // 4)
    cy = random.randint(h // 4, 3 * h // 4)
    r = random.randint(30, 60)
    for i in range(r - 3, r + 3):
        color = (210, 195, 170, 40)  # light brown, very faint
        draw.ellipse([cx - i, cy - i, cx + i, cy + i], outline=(215, 200, 180))


def generate_receipt(
    items: list,
    style: dict,
    filename: str,
    title: str = "RECEIPT",
):
    """
    Generate one receipt image.

    items: list of (code, qty) tuples
    style: dict with keys: bg_color, ink_color, font_size, jitter, rotation,
           line_spacing, smudges, faded, coffee_stain
    """
    w, h = style.get("size", (850, 1100))
    bg = style.get("bg_color", (235, 230, 220))
    ink = style.get("ink_color", (25, 20, 40))
    header_ink = style.get("header_ink", (15, 10, 30))
    font_size = style.get("font_size", 34)
    jitter_amt = style.get("jitter", 2)
    rotation = style.get("rotation", 1.5)
    line_spacing = style.get("line_spacing", 48)
    add_smudges = style.get("smudges", 2)
    faded = style.get("faded", False)
    add_coffee = style.get("coffee_stain", False)
    ruled = style.get("ruled_lines", True)

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    font = get_font(font_size)
    font_sm = get_font(int(font_size * 0.75))
    font_lg = get_font(int(font_size * 1.2))

    if faded:
        # Lighten ink for faded effect (mild — still readable)
        ink = tuple(min(255, c + 45) for c in ink)
        header_ink = tuple(min(255, c + 35) for c in header_ink)

    # Add imperfections
    if add_smudges:
        _add_smudge(img, add_smudges)
    if add_coffee:
        _add_coffee_stain(img)

    y = 40

    # Title
    tx, ty = _jitter(w // 2 - 60, y, jitter_amt)
    draw.text((tx, ty), title, fill=header_ink, font=font_lg)
    y += int(line_spacing * 1.3)

    # Date
    dx, dy = _jitter(50, y, jitter_amt)
    draw.text((dx, dy), "Date: 04/03/2026", fill=ink, font=font_sm)
    y += line_spacing

    # Separator line
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
    y += 15

    # Column headers
    hx2, hy2 = _jitter(80, y, jitter_amt)
    hx3, hy3 = _jitter(500, y, jitter_amt)
    draw.text((hx2, hy2), "Item Code", fill=header_ink, font=font_sm)
    draw.text((hx3, hy3), "Qty", fill=header_ink, font=font_sm)
    y += int(line_spacing * 0.9)

    # Separator
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
    y += 12

    # Draw each item
    for idx, (code, qty) in enumerate(items, 1):
        if ruled:
            _draw_wavy_line(draw, y + int(line_spacing * 0.85), w - 60,
                            (200, 195, 185), 1)

        # Code + Qty — draw as one continuous handwritten line
        # e.g. "TEW1    3" so OCR reads them together
        full_line = f"{code}    {qty}"
        cx = 80
        for ch in full_line:
            if ch == ' ':
                cx += random.randint(18, 28)  # natural hand-gap for spaces
                continue
            chx, chy = _jitter(cx, y, jitter_amt)
            draw.text((chx, chy), ch, fill=ink, font=font)
            # Get character width
            bbox = font.getbbox(ch)
            cx += bbox[2] - bbox[0] + random.randint(-1, 2)

        y += line_spacing

    # Bottom separator
    y += 5
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)

    # Paper texture
    _add_paper_texture(img, intensity=style.get("texture_intensity", 8))

    # Slight rotation
    if rotation:
        angle = random.uniform(-rotation, rotation)
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False,
                         fillcolor=bg)

    # Optional slight blur for faded effect
    if faded:
        img = img.filter(ImageFilter.GaussianBlur(radius=0.7))

    out_path = OUTPUT_DIR / filename
    img.save(str(out_path), "JPEG", quality=88)
    print(f"  ✓ Generated: {out_path}  ({w}x{h})")
    return str(out_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Define test receipts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RECEIPTS = [
    {
        "name": "receipt_neat.jpg",
        "title": "PAINT STORE",
        "items": [
            ("TEW1", 3),
            ("TEW4", 2),
            ("PEPW10", 5),
            ("PEPW20", 1),
        ],
        "style": {
            "bg_color": (220, 215, 205),
            "ink_color": (10, 8, 25),
            "font_size": 38,
            "jitter": 1,
            "rotation": 0.8,
            "line_spacing": 55,
            "smudges": 0,
            "texture_intensity": 4,
        },
    },
    {
        "name": "receipt_messy.jpg",
        "title": "RECEIPT",
        "items": [
            ("TEW10", 2),
            ("TEW20", 4),
            ("PEPW1", 6),
            ("PEPW4", 3),
        ],
        "style": {
            "bg_color": (215, 210, 200),
            "ink_color": (15, 12, 30),
            "font_size": 38,
            "jitter": 4,
            "rotation": 2.5,
            "line_spacing": 58,
            "smudges": 2,
            "coffee_stain": True,
            "texture_intensity": 6,
        },
    },
    {
        "name": "receipt_faded.jpg",
        "title": "STORE RECEIPT",
        "items": [
            ("TEW1", 1),
            ("TEW10", 3),
            ("PEPW1", 2),
            ("PEPW10", 4),
        ],
        "style": {
            "bg_color": (218, 213, 203),
            "ink_color": (40, 35, 55),
            "font_size": 36,
            "jitter": 2,
            "rotation": 1.2,
            "line_spacing": 52,
            "smudges": 1,
            "faded": True,
            "texture_intensity": 5,
        },
    },
    {
        "name": "receipt_dense.jpg",
        "title": "PAINT ORDER",
        "items": [
            ("TEW1", 2),
            ("TEW4", 5),
            ("TEW10", 1),
            ("TEW20", 3),
            ("PEPW1", 4),
            ("PEPW4", 2),
            ("PEPW10", 6),
            ("PEPW20", 1),
        ],
        "style": {
            "bg_color": (210, 206, 196),
            "ink_color": (8, 5, 18),
            "font_size": 38,
            "jitter": 2,
            "rotation": 1.5,
            "line_spacing": 54,
            "smudges": 0,
            "texture_intensity": 4,
            "size": (900, 1400),
        },
    },
    {
        "name": "receipt_dark_ink.jpg",
        "title": "RECEIPT",
        "items": [
            ("PEPW20", 3),
            ("TEW4", 7),
            ("PEPW10", 2),
            ("TEW1", 5),
            ("PEPW4", 1),
        ],
        "style": {
            "bg_color": (212, 208, 198),
            "ink_color": (8, 5, 18),
            "header_ink": (5, 2, 12),
            "font_size": 38,
            "jitter": 2,
            "rotation": 1.5,
            "line_spacing": 55,
            "smudges": 1,
            "texture_intensity": 5,
        },
    },
]


if __name__ == "__main__":
    random.seed(42)  # Fixed seed for reproducible images
    print("=" * 55)
    print("  Generating Handwritten Receipt Test Images")
    print("=" * 55)
    paths = []
    for r in RECEIPTS:
        p = generate_receipt(
            items=r["items"],
            style=r["style"],
            filename=r["name"],
            title=r["title"],
        )
        paths.append(p)
    print(f"\n✓ {len(paths)} images saved to {OUTPUT_DIR}/")
