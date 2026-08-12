"""
Generate a mobile-optimized 'after' worksheet image.

ANALYSIS FINDINGS (why hero.png works but after.png is blurry):
  hero.png  main text band = 112px source → 33.9px at 310px display  ✓ readable
  after.png text lines    = 23-44px source →  7-13px at 310px display ✗ too small
  72% of hero strokes & 54% of after strokes are <4px → vanish at 310px.

The hero survives because its *important* content (title + big illustration) is
large. The after fails because 6 lines of small serif text all fall below the
~15px display-size legibility floor after 3.3x downscaling.

FIX: render a worksheet with fewer (3) lines of MUCH larger text, so every line
clears the legibility floor. Source font 48px → 14.5px display. Deterministic
PIL render gives exact control over the one variable that matters: font size.
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
import os, random

random.seed(42)
np.random.seed(42)

W, H = 1024, 1536

# ---- AGED PAPER BACKGROUND -------------------------------------------------
base = np.full((H, W, 3), [250, 243, 224], dtype=np.int32)  # warm cream

# gentle vertical gradient (slightly darker at top/bottom edges)
grad = np.linspace(0, 1, H).reshape(H, 1, 1)
edge_dark = (np.abs(grad - 0.5) * 2 * 10).astype(np.int32)  # 0 center → 10 edges
base = base - edge_dark

# paper grain
grain = np.random.normal(0, 2.5, (H, W, 3)).astype(np.int32)
base = base + grain

# a few faint tea-stain blotches
for _ in range(5):
    cx, cy = random.randint(0, W), random.randint(0, H)
    r = random.randint(80, 200)
    yy, xx = np.ogrid[:H, :W]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    stain = np.clip(1 - dist / r, 0, 1) * 14  # subtle darkening
    base = base - stain[:, :, None] * np.array([8, 6, 4])

base = np.clip(base, 0, 255).astype(np.uint8)
paper = Image.fromarray(base)

# ---- FONTS -----------------------------------------------------------------
BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"
REG  = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
ITAL = "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"

# Sizing math: source 1024px → 310px display = 3.305x downscale.
# To get a 20px display glyph, need 66px source. We round up generously.
f_title   = ImageFont.truetype(BOLD, 78)   # → 23.6px display  (title)
f_sub     = ImageFont.truetype(REG,  50)   # → 15.1px display  (lesson line)
f_instr   = ImageFont.truetype(ITAL, 46)   # → 13.9px display  (instruction)
f_num     = ImageFont.truetype(BOLD, 54)   # → 16.3px display  (numerals)
f_body    = ImageFont.truetype(REG,  52)   # → 15.7px display  (sentence text)
f_footer  = ImageFont.truetype(REG,  34)   # → 10.3px display

INK    = (38, 28, 18)      # dark sepia ink
MUTED  = (95, 75, 52)      # faded brown
RUST   = (135, 55, 32)     # correction red

draw = ImageDraw.Draw(paper)

# ---- HEADER ---------------------------------------------------------------
MARGIN_L = 130
MARGIN_R = W - 130

draw.text((MARGIN_L, 95), "GRAMMAR EXERCISE", font=f_title, fill=INK)
# subtitle with a little letter spacing
sub = "L E S S O N   X I I   ·   P A G E   4"
draw.text((MARGIN_L, 188), sub, font=f_sub, fill=MUTED)

# decorative double rule
draw.rectangle([MARGIN_L, 268, MARGIN_R, 272], fill=INK)
draw.rectangle([MARGIN_L, 280, MARGIN_R, 281], fill=MUTED)

# ---- INSTRUCTION ----------------------------------------------------------
draw.text((MARGIN_L, 315), "Underline the verb in each sentence.",
          font=f_instr, fill=MUTED)

# ---- THREE SENTENCES (large, generously spaced) ---------------------------
sentences = [
    ("1.", "The squirrel gathered nuts all day long.", 0.34, 0.60),
    ("2.", "Peter ran under the garden gate.",         0.30, 0.52),
    ("3.", "Mrs. Rabbit baked a fresh pie.",           0.45, 0.72),
]

y = 460
ROW = 250  # generous vertical rhythm → text dominates the canvas

for num, text, vstart, vend in sentences:
    draw.text((MARGIN_L, y), num, font=f_num, fill=INK)
    text_x = MARGIN_L + 95
    draw.text((text_x, y), text, font=f_body, fill=INK)

    # hand-drawn wavy underline under the verb region
    tw = draw.textlength(text, font=f_body)
    ux0 = text_x + tw * vstart
    ux1 = text_x + tw * vend
    uy = y + 70
    xs = np.arange(ux0, ux1, 3)
    for i, x in enumerate(xs):
        wob = 2.4 * np.sin(i * 0.55)
        draw.line([x, uy + wob, x + 3, uy - wob], fill=RUST, width=6)
    # small tick at end (like a teacher's pen lift)
    draw.line([ux1, uy - 4, ux1 + 5, uy + 6], fill=RUST, width=5)

    y += ROW

# ---- SMALL WOODCUT-STYLE ILLUSTRATION (simple rabbit silhouette) ----------
# Drawn large enough (~180px) to read as an illustration, not noise.
ill = Image.new("RGBA", (220, 240), (0, 0, 0, 0))
idr = ImageDraw.Draw(ill)
# body
idr.ellipse([20, 110, 200, 230], fill=(38, 28, 18, 255))
# head
idr.ellipse([130, 60, 210, 140], fill=(38, 28, 18, 255))
# ears
idr.polygon([(150, 70), (140, 0), (172, 60)], fill=(38, 28, 18, 255))
idr.polygon([(178, 68), (185, 5), (200, 66)], fill=(38, 28, 18, 255))
# eye (paper-colored notch)
idr.ellipse([178, 92, 192, 104], fill=(250, 243, 224, 255))
# tail
idr.ellipse([5, 150, 55, 195], fill=(38, 28, 18, 255))
# slight roughen so it looks printed, not vector-perfect
ill = ill.filter(ImageFilter.GaussianBlur(0.4))
paper.paste(ill, (MARGIN_L, y + 10), ill)

# ---- FOOTER ---------------------------------------------------------------
fy = H - 150
draw.rectangle([MARGIN_L, fy - 40, MARGIN_R, fy - 38], fill=MUTED)
draw.text((MARGIN_L, fy), "Restored by Book Bash  ·  2026", font=f_footer, fill=MUTED)
# page number, right aligned
pn = "4"
pnw = draw.textlength(pn, font=f_num)
draw.text((MARGIN_R - pnw, fy - 8), pn, font=f_num, fill=MUTED)

# ---- SAVE -----------------------------------------------------------------
paper.save("public/images/after.png", optimize=True)
print(f"Saved public/images/after.png ({os.path.getsize('public/images/after.png')//1024} KB)")

# Retina/2x variant kept same; we'll use srcset to also offer a 620px variant
# generated from this same source (already >2x density at 310px).

# ---- VERIFY: simulate mobile downscale & report text heights --------------
small = paper.resize((310, int(H * 310 / W)), Image.LANCZOS)
small.save("/tmp/after_new_mobile.png")
print(f"Mobile preview: /tmp/after_new_mobile.png  {small.size}")

arr = np.array(paper.convert("L"))
ink = arr < 110
row_ink = ink.mean(axis=1)
print("\nText-band heights (source → 310px display):")
in_band = False
start = 0
for i, d in enumerate(row_ink):
    if d > 0.03 and not in_band:
        in_band, start = True, i
    elif d <= 0.03 and in_band:
        if i - start >= 12:
            print(f"  rows {start:4d}-{i:4d}: {i-start:3d}px → {(i-start)*310/1024:5.1f}px display")
        in_band = False
