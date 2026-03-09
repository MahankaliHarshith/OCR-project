"""
Generate a realistic handwritten-style receipt image for testing OCR accuracy.
Uses random slight offsets, varying font sizes, and a slightly rotated/noisy
background to simulate real handwriting on paper.
"""

import random
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageTransform

# ── Receipt content (new TE/PEP products) ──
RECEIPT_ITEMS = [
    ("TEW1",   3),
    ("TEW4",   2),
    ("TEW10",  5),
    ("TEW20",  1),
    ("PEPW1",  4),
    ("PEPW4",  2),
    ("PEPW10", 3),
    ("PEPW20", 1),
]

def create_handwritten_receipt(output_path: str):
    """Create a receipt image that looks handwritten."""
    W, H = 800, 1000
    # Light gray paper background (not too bright — avoids overexposure)
    img = Image.new("RGB", (W, H), (235, 230, 220))
    draw = ImageDraw.Draw(img)

    # Add paper texture (random dots)
    random.seed(42)
    for _ in range(3000):
        x = random.randint(0, W - 1)
        y = random.randint(0, H - 1)
        shade = random.randint(215, 235)
        draw.point((x, y), fill=(shade, shade, shade - 5))

    # Try to use a handwriting-like font, fall back to default
    fonts = []
    font_names = [
        "C:/Windows/Fonts/comic.ttf",       # Comic Sans (handwriting-ish)
        "C:/Windows/Fonts/segoepr.ttf",      # Segoe Print
        "C:/Windows/Fonts/segoesc.ttf",      # Segoe Script
        "C:/Windows/Fonts/arial.ttf",        # Fallback
    ]
    
    main_font = None
    title_font = None
    for fn in font_names:
        try:
            main_font = ImageFont.truetype(fn, 36)
            title_font = ImageFont.truetype(fn, 44)
            print(f"Using font: {fn}")
            break
        except (OSError, IOError):
            continue
    
    if main_font is None:
        main_font = ImageFont.load_default()
        title_font = main_font
        print("Using default font")

    # ── Ink colors (dark blue/black variations like pen ink) ──
    ink_colors = [
        (5, 5, 30),      # Very dark blue-black
        (10, 10, 25),
        (0, 0, 40),
        (15, 5, 35),
        (0, 0, 45),
    ]

    y_pos = 60

    # ── Title ──
    title = "Items List"
    color = random.choice(ink_colors)
    # Slight random offset for handwritten feel
    draw.text((W // 2 - 100 + random.randint(-5, 5), y_pos + random.randint(-3, 3)),
              title, fill=color, font=title_font)
    y_pos += 70

    # ── Underline (wavy) ──
    for x in range(120, W - 120):
        wave_y = y_pos + int(1.5 * math.sin(x / 15))
        draw.point((x, wave_y), fill=random.choice(ink_colors))
        draw.point((x, wave_y + 1), fill=random.choice(ink_colors))
    y_pos += 40

    # ── Column headers ──
    color = random.choice(ink_colors)
    draw.text((80 + random.randint(-3, 3), y_pos + random.randint(-2, 2)),
              "S.No", fill=color, font=main_font)
    draw.text((220 + random.randint(-3, 3), y_pos + random.randint(-2, 2)),
              "Code", fill=color, font=main_font)
    draw.text((520 + random.randint(-3, 3), y_pos + random.randint(-2, 2)),
              "Qty", fill=color, font=main_font)
    y_pos += 55

    # ── Separator line ──
    for x in range(70, W - 70):
        if random.random() > 0.08:  # Slight gaps like real pen
            wave_y = y_pos + int(1 * math.sin(x / 20))
            draw.point((x, wave_y), fill=random.choice(ink_colors))
    y_pos += 25

    # ── Write each item ──
    for idx, (code, qty) in enumerate(RECEIPT_ITEMS, 1):
        color = random.choice(ink_colors)
        
        # Random vertical jitter (handwriting isn't perfectly aligned)
        y_jitter = random.randint(-4, 4)
        x_jitter_sno = random.randint(-6, 6)
        x_jitter_code = random.randint(-8, 8)
        x_jitter_qty = random.randint(-6, 6)

        # S.No
        draw.text((95 + x_jitter_sno, y_pos + y_jitter),
                  f"{idx}.", fill=color, font=main_font)
        
        # Product code — always uppercase for clarity
        code_display = code
        
        draw.text((210 + x_jitter_code, y_pos + y_jitter + random.randint(-2, 2)),
                  code_display, fill=color, font=main_font)

        # Dash separator
        dash_x = 420 + random.randint(-10, 10)
        draw.text((dash_x, y_pos + y_jitter), "-", fill=color, font=main_font)

        # Quantity
        draw.text((530 + x_jitter_qty, y_pos + y_jitter + random.randint(-2, 2)),
                  str(qty), fill=color, font=main_font)
        
        y_pos += random.randint(70, 85)  # Variable line spacing

    # ── Footer ──
    y_pos += 20
    for x in range(70, W - 70):
        if random.random() > 0.1:
            wave_y = y_pos + int(1 * math.sin(x / 18))
            draw.point((x, wave_y), fill=random.choice(ink_colors))
    y_pos += 35
    
    color = random.choice(ink_colors)
    draw.text((350 + random.randint(-5, 5), y_pos + random.randint(-3, 3)),
              f"Total Items: {len(RECEIPT_ITEMS)}", fill=color, font=main_font)

    # ── Post-processing: slight blur + noise for realism ──
    img = img.filter(ImageFilter.GaussianBlur(radius=0.7))
    
    # Add slight rotation (like a slightly crooked scan)
    angle = random.uniform(-1.5, 1.5)
    img = img.rotate(angle, fillcolor=(235, 230, 220), expand=False)

    img.save(output_path, quality=92)
    print(f"\nReceipt image saved to: {output_path}")
    print(f"Items: {len(RECEIPT_ITEMS)}")
    print(f"Image size: {img.size}")
    return output_path


if __name__ == "__main__":
    path = create_handwritten_receipt("test_receipt_tepep.jpg")
