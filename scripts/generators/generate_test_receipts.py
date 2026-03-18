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
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUTPUT_DIR = Path("test_images")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Fonts ─────────────────────────────────────────────────────────────────────
# Try multiple handwriting-like fonts, fallback to default
def get_font(size: int):
    for name in ["comic.ttf", "comicbd.ttf", "segoesc.ttf", "calibri.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
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
        draw.ellipse([cx - i, cy - i, cx + i, cy + i], outline=(215, 200, 180))


def generate_receipt(
    items: list,
    style: dict,
    filename: str,
    title: str = "RECEIPT",
):
    """
    Generate one receipt image with 4-column layout:
        CODE   QTY   RATE   AMOUNT

    items: list of (code, qty, rate) tuples.
           If rate is provided, renders full 4-col layout + Grand Total.
           Otherwise, falls back to 2-col (code, qty) + Total Qty.
    style: dict with rendering parameters.
    """
    # Determine if we have priced items (3-tuple)
    has_prices = len(items[0]) >= 3 if items else False

    w, h = style.get("size", (1000, 1200))
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
        ink = tuple(min(255, c + 45) for c in ink)
        header_ink = tuple(min(255, c + 35) for c in header_ink)

    # Add imperfections
    if add_smudges:
        _add_smudge(img, add_smudges)
    if add_coffee:
        _add_coffee_stain(img)

    y = 40

    # Title
    tx, ty = _jitter(w // 2 - 80, y, jitter_amt)
    draw.text((tx, ty), title, fill=header_ink, font=font_lg)
    y += int(line_spacing * 1.3)

    # Date
    dx, dy = _jitter(50, y, jitter_amt)
    draw.text((dx, dy), "Date: 04/03/2026", fill=ink, font=font_sm)
    y += line_spacing

    # Separator line
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
    y += 15

    if has_prices:
        # 4-column headers: CODE  QTY  RATE  AMOUNT
        col_positions = [80, 350, 520, 720]
        headers = ["Item Code", "Qty", "Rate", "Amount"]
        for i, hdr in enumerate(headers):
            hx, hy = _jitter(col_positions[i], y, jitter_amt)
            draw.text((hx, hy), hdr, fill=header_ink, font=font_sm)
    else:
        # 2-column headers
        hx2, hy2 = _jitter(80, y, jitter_amt)
        hx3, hy3 = _jitter(500, y, jitter_amt)
        draw.text((hx2, hy2), "Item Code", fill=header_ink, font=font_sm)
        draw.text((hx3, hy3), "Qty", fill=header_ink, font=font_sm)

    y += int(line_spacing * 0.9)

    # Separator
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
    y += 12

    # Draw each item
    for _idx, item_tuple in enumerate(items, 1):
        if ruled:
            _draw_wavy_line(draw, y + int(line_spacing * 0.85), w - 60,
                            (200, 195, 185), 1)

        if has_prices:
            code, qty, rate = item_tuple[0], item_tuple[1], item_tuple[2]
            amount = qty * rate
            # Draw 4 columns with character-by-character jitter
            col_texts = [str(code), str(qty), str(int(rate)), str(int(amount))]
            col_x = [80, 370, 530, 730]
            for ci, text in enumerate(col_texts):
                cx = col_x[ci]
                for ch in text:
                    if ch == ' ':
                        cx += random.randint(18, 28)
                        continue
                    chx, chy = _jitter(cx, y, jitter_amt)
                    draw.text((chx, chy), ch, fill=ink, font=font)
                    bbox = font.getbbox(ch)
                    cx += bbox[2] - bbox[0] + random.randint(-1, 2)
        else:
            code, qty = item_tuple[0], item_tuple[1]
            full_line = f"{code}    {qty}"
            cx = 80
            for ch in full_line:
                if ch == ' ':
                    cx += random.randint(18, 28)
                    continue
                chx, chy = _jitter(cx, y, jitter_amt)
                draw.text((chx, chy), ch, fill=ink, font=font)
                bbox = font.getbbox(ch)
                cx += bbox[2] - bbox[0] + random.randint(-1, 2)

        y += line_spacing

    # Separator before total
    y += 5
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
    y += 15

    if has_prices:
        # Total Qty line
        total_qty = sum(item[1] for item in items)
        total_line = f"Total Qty    {total_qty}"
        tx = 80
        for ch in total_line:
            if ch == ' ':
                tx += random.randint(18, 28)
                continue
            chx, chy = _jitter(tx, y, jitter_amt)
            draw.text((chx, chy), ch, fill=header_ink, font=font)
            bbox = font.getbbox(ch)
            tx += bbox[2] - bbox[0] + random.randint(-1, 2)
        y += line_spacing

        # Grand Total line
        grand_total = sum(item[1] * item[2] for item in items)
        grand_line = f"Grand Total    {int(grand_total)}"
        gx = 80
        for ch in grand_line:
            if ch == ' ':
                gx += random.randint(18, 28)
                continue
            chx, chy = _jitter(gx, y, jitter_amt)
            draw.text((chx, chy), ch, fill=header_ink, font=font)
            bbox = font.getbbox(ch)
            gx += bbox[2] - bbox[0] + random.randint(-1, 2)
        y += line_spacing
    else:
        # Total Qty line only
        total_qty = sum(item[1] for item in items)
        total_line = f"Total Qty    {total_qty}"
        tx = 80
        for ch in total_line:
            if ch == ' ':
                tx += random.randint(18, 28)
                continue
            chx, chy = _jitter(tx, y, jitter_amt)
            draw.text((chx, chy), ch, fill=header_ink, font=font)
            bbox = font.getbbox(ch)
            tx += bbox[2] - bbox[0] + random.randint(-1, 2)
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
            ("TEW1", 3, 250),
            ("TEW4", 2, 850),
            ("PEPW10", 5, 2600),
            ("PEPW20", 1, 4800),
        ],
        "style": {
            "bg_color": (220, 215, 205),
            "ink_color": (10, 8, 25),
            "font_size": 34,
            "jitter": 1,
            "rotation": 0.8,
            "line_spacing": 55,
            "smudges": 0,
            "texture_intensity": 4,
            "size": (1000, 1200),
        },
    },
    {
        "name": "receipt_messy.jpg",
        "title": "RECEIPT",
        "items": [
            ("TEW10", 2, 1800),
            ("TEW20", 4, 3200),
            ("PEPW1", 6, 350),
            ("PEPW4", 3, 1200),
        ],
        "style": {
            "bg_color": (215, 210, 200),
            "ink_color": (15, 12, 30),
            "font_size": 34,
            "jitter": 4,
            "rotation": 2.5,
            "line_spacing": 58,
            "smudges": 2,
            "coffee_stain": True,
            "texture_intensity": 6,
            "size": (1000, 1200),
        },
    },
    {
        "name": "receipt_faded.jpg",
        "title": "STORE RECEIPT",
        "items": [
            ("TEW1", 1, 250),
            ("TEW10", 3, 1800),
            ("PEPW1", 2, 350),
            ("PEPW10", 4, 2600),
        ],
        "style": {
            "bg_color": (218, 213, 203),
            "ink_color": (40, 35, 55),
            "font_size": 33,
            "jitter": 2,
            "rotation": 1.2,
            "line_spacing": 52,
            "smudges": 1,
            "faded": True,
            "texture_intensity": 5,
            "size": (1000, 1200),
        },
    },
    {
        "name": "receipt_dense.jpg",
        "title": "PAINT ORDER",
        "items": [
            ("TEW1", 2, 250),
            ("TEW4", 5, 850),
            ("TEW10", 1, 1800),
            ("TEW20", 3, 3200),
            ("PEPW1", 4, 350),
            ("PEPW4", 2, 1200),
            ("PEPW10", 6, 2600),
            ("PEPW20", 1, 4800),
        ],
        "style": {
            "bg_color": (210, 206, 196),
            "ink_color": (8, 5, 18),
            "font_size": 32,
            "jitter": 2,
            "rotation": 1.5,
            "line_spacing": 50,
            "smudges": 0,
            "texture_intensity": 4,
            "size": (1050, 1500),
        },
    },
    {
        "name": "receipt_dark_ink.jpg",
        "title": "RECEIPT",
        "items": [
            ("PEPW20", 3, 4800),
            ("TEW4", 7, 850),
            ("PEPW10", 2, 2600),
            ("TEW1", 5, 250),
            ("PEPW4", 1, 1200),
        ],
        "style": {
            "bg_color": (212, 208, 198),
            "ink_color": (8, 5, 18),
            "header_ink": (5, 2, 12),
            "font_size": 34,
            "jitter": 2,
            "rotation": 1.5,
            "line_spacing": 55,
            "smudges": 1,
            "texture_intensity": 5,
            "size": (1000, 1300),
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
