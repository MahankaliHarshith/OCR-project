"""
Generate edge-case receipt images to deep-audit the total verification feature.

Covers scenarios:
  1. Single item receipt (qty=1)
  2. No total line at all
  3. Large quantities (10-50 per item)
  4. Pure-alpha codes only (ABC, DEF, GHI)
  5. All 18 products on one receipt
  6. All items qty=1
  7. Double-digit quantities (10, 15, 20)
  8. Mixed with "Total Items" footer (potential false positive)
  9. Extreme minimal receipt (1 item qty=1)
  10. High total (sum > 50)
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUTPUT_DIR = Path("test_images")
OUTPUT_DIR.mkdir(exist_ok=True)


def get_font(size: int):
    for name in ["comic.ttf", "comicbd.ttf", "segoesc.ttf", "calibri.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _jitter(x, y, amount=2):
    return x + random.randint(-amount, amount), y + random.randint(-amount, amount)


def _draw_wavy_line(draw, y, width, color, thickness=1):
    pts = []
    for x in range(0, width, 4):
        pts.append((x, y + random.randint(-1, 1)))
    if len(pts) > 1:
        draw.line(pts, fill=color, width=thickness)


def _add_paper_texture(img, intensity=8):
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


def generate_receipt(
    items: list,
    style: dict,
    filename: str,
    title: str = "RECEIPT",
    total_line: str = "auto",   # "auto" = "Total Qty N", None = no total line, or custom string
    extra_footer: str = None,   # optional extra line after total (e.g., "Total Items: 3")
):
    """
    Generate one receipt image with configurable total line.

    items: list of (code, qty) tuples
    style: dict with rendering options
    total_line: "auto" computes total, None skips, or provide custom text
    extra_footer: optional footer text after total line
    """
    w, h = style.get("size", (850, 1100))
    bg = style.get("bg_color", (235, 230, 220))
    ink = style.get("ink_color", (25, 20, 40))
    header_ink = style.get("header_ink", (15, 10, 30))
    font_size = style.get("font_size", 34)
    jitter_amt = style.get("jitter", 2)
    rotation = style.get("rotation", 1.5)
    line_spacing = style.get("line_spacing", 48)
    faded = style.get("faded", False)

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    font = get_font(font_size)
    font_sm = get_font(int(font_size * 0.75))
    font_lg = get_font(int(font_size * 1.2))

    if faded:
        ink = tuple(min(255, c + 45) for c in ink)
        header_ink = tuple(min(255, c + 35) for c in header_ink)

    y = 40

    # Title
    tx, ty = _jitter(w // 2 - 80, y, jitter_amt)
    draw.text((tx, ty), title, fill=header_ink, font=font_lg)
    y += int(line_spacing * 1.3)

    # Date
    dx, dy = _jitter(50, y, jitter_amt)
    draw.text((dx, dy), "Date: 05/06/2026", fill=ink, font=font_sm)
    y += line_spacing

    # Separator
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
    for _idx, (code, qty) in enumerate(items, 1):
        _draw_wavy_line(draw, y + int(line_spacing * 0.85), w - 60, (200, 195, 185), 1)

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

    # Total Qty line
    if total_line == "auto":
        total_qty = sum(qty for _, qty in items)
        total_text = f"Total Qty    {total_qty}"
    elif total_line is not None:
        total_text = total_line
    else:
        total_text = None

    if total_text:
        tx = 80
        for ch in total_text:
            if ch == ' ':
                tx += random.randint(18, 28)
                continue
            chx, chy = _jitter(tx, y, jitter_amt)
            draw.text((chx, chy), ch, fill=header_ink, font=font)
            bbox = font.getbbox(ch)
            tx += bbox[2] - bbox[0] + random.randint(-1, 2)
        y += line_spacing

    # Extra footer (e.g., "Total Items: 3") — add MORE spacing to prevent OCR line merging
    if extra_footer:
        y += 20  # extra vertical gap between total and footer
        _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)
        y += 15
        fx = 80
        for ch in extra_footer:
            if ch == ' ':
                fx += random.randint(14, 22)
                continue
            fxj, fyj = _jitter(fx, y, jitter_amt)
            draw.text((fxj, fyj), ch, fill=ink, font=font_sm)
            bbox = font_sm.getbbox(ch)
            fx += bbox[2] - bbox[0] + random.randint(-1, 2)
        y += line_spacing

    # Bottom separator
    y += 5
    _draw_wavy_line(draw, y, w - 60, (*ink[:3],), 1)

    # Paper texture
    _add_paper_texture(img, intensity=style.get("texture_intensity", 8))

    # Slight rotation
    if rotation:
        angle = random.uniform(-rotation, rotation)
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=bg)

    # Optional slight blur for faded effect
    if faded:
        img = img.filter(ImageFilter.GaussianBlur(radius=0.7))

    out_path = OUTPUT_DIR / filename
    img.save(str(out_path), "JPEG", quality=88)
    print(f"  ✓ Generated: {out_path}  ({w}x{h})")
    return str(out_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Edge-Case Receipts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STYLE_CLEAN = {
    "bg_color": (220, 215, 205),
    "ink_color": (10, 8, 25),
    "font_size": 38,
    "jitter": 1,
    "rotation": 0.8,
    "line_spacing": 55,
    "texture_intensity": 4,
}

STYLE_MESSY = {
    "bg_color": (215, 210, 200),
    "ink_color": (15, 12, 30),
    "font_size": 38,
    "jitter": 3,
    "rotation": 2.0,
    "line_spacing": 55,
    "texture_intensity": 6,
}

EDGE_CASES = [
    # 1. Single item receipt
    {
        "name": "edge_single_item.jpg",
        "title": "RECEIPT",
        "items": [("TEW1", 5)],
        "style": STYLE_CLEAN,
        "total_line": "auto",
        "expected_total": 5,
        "expected_items": 1,
    },
    # 2. No total line
    {
        "name": "edge_no_total.jpg",
        "title": "PAINT STORE",
        "items": [("TEW4", 3), ("PEPW10", 2), ("TEW20", 1)],
        "style": STYLE_CLEAN,
        "total_line": None,
        "expected_total": None,  # no total line → verification_status = not_found
        "expected_items": 3,
    },
    # 3. Large quantities
    {
        "name": "edge_large_qty.jpg",
        "title": "BULK ORDER",
        "items": [("TEW10", 15), ("TEW20", 20), ("PEPW4", 10)],
        "style": STYLE_CLEAN,
        "total_line": "auto",
        "expected_total": 45,
        "expected_items": 3,
    },
    # 4. Pure-alpha codes only
    {
        "name": "edge_alpha_codes.jpg",
        "title": "STORE RECEIPT",
        "items": [("ABC", 2), ("DEF", 3), ("GHI", 4)],
        "style": STYLE_CLEAN,
        "total_line": "auto",
        "expected_total": 9,
        "expected_items": 3,
    },
    # 5. All qty=1
    {
        "name": "edge_all_qty1.jpg",
        "title": "RECEIPT",
        "items": [("TEW1", 1), ("TEW4", 1), ("PEPW1", 1), ("PEPW4", 1)],
        "style": STYLE_CLEAN,
        "total_line": "auto",
        "expected_total": 4,
        "expected_items": 4,
    },
    # 6. Double-digit quantities
    {
        "name": "edge_double_digit.jpg",
        "title": "PAINT ORDER",
        "items": [("TEW1", 10), ("PEPW20", 12), ("TEW10", 15)],
        "style": STYLE_CLEAN,
        "total_line": "auto",
        "expected_total": 37,
        "expected_items": 3,
    },
    # 7. Many items (8 products) — larger canvas + wider spacing for OCR clarity
    {
        "name": "edge_many_items.jpg",
        "title": "BIG ORDER",
        "items": [
            ("TEW1", 2), ("TEW4", 3), ("TEW10", 1), ("TEW20", 4),
            ("PEPW1", 5), ("PEPW4", 2), ("PEPW10", 3), ("PEPW20", 1),
        ],
        "style": {**STYLE_CLEAN, "size": (950, 1600), "line_spacing": 60, "font_size": 40},
        "total_line": "auto",
        "expected_total": 21,
        "expected_items": 8,
    },
    # 8. "Total Items" false positive test
    # This receipt has "Total Qty 8" AND "Total Items: 3" —
    # the scanner should use "Total Qty 8", NOT "Total Items: 3"
    {
        "name": "edge_total_items_confusion.jpg",
        "title": "RECEIPT",
        "items": [("TEW1", 3), ("PEPW10", 2), ("TEW20", 3)],
        "style": STYLE_CLEAN,
        "total_line": "auto",           # will be "Total Qty 8"
        "extra_footer": "Total Items: 3",  # footer label — should be ignored
        "expected_total": 8,
        "expected_items": 3,
    },
    # 9. Mixed alpha + alphanumeric codes
    {
        "name": "edge_mixed_codes.jpg",
        "title": "PAINT STORE",
        "items": [("ABC", 3), ("TEW10", 2), ("DEF", 1), ("PEPW1", 4)],
        "style": STYLE_MESSY,
        "total_line": "auto",
        "expected_total": 10,
        "expected_items": 4,
    },
    # 10. High total (sum > 50) — wider spacing for double-digit quantities
    {
        "name": "edge_high_total.jpg",
        "title": "WAREHOUSE ORDER",
        "items": [("TEW1", 10), ("TEW4", 12), ("TEW10", 8), ("PEPW20", 15), ("PEPW10", 10)],
        "style": {**STYLE_CLEAN, "size": (950, 1400), "line_spacing": 58, "font_size": 40},
        "total_line": "auto",
        "expected_total": 55,
        "expected_items": 5,
    },
]


if __name__ == "__main__":
    random.seed(99)  # Fixed seed for reproducibility
    print("=" * 60)
    print("  Generating Edge-Case Receipt Images for Deep Audit")
    print("=" * 60)
    paths = []
    for r in EDGE_CASES:
        p = generate_receipt(
            items=r["items"],
            style=r["style"],
            filename=r["name"],
            title=r["title"],
            total_line=r.get("total_line", "auto"),
            extra_footer=r.get("extra_footer"),
        )
        paths.append(p)
    print(f"\n✓ {len(paths)} edge-case images saved to {OUTPUT_DIR}/")
    print("\nExpected results:")
    for r in EDGE_CASES:
        et = r['expected_total']
        ei = r['expected_items']
        print(f"  {r['name']}: {ei} items, total={et if et else 'NO TOTAL LINE'}")
