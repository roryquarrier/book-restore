#!/usr/bin/env python3
"""Step 3: Install chosen after (clean) + before (degraded) as png+webp."""
import sys
from pathlib import Path
from PIL import Image

IMG_DIR = Path("/home/hal/book-bash/public/images")

def install(after_src, before_src):
    after_src, before_src = Path(after_src), Path(before_src)
    for name in ("after", "before"):
        for ext in ("png", "webp"):
            pass
    # after
    a_png = IMG_DIR / "after.png"
    a_webp = IMG_DIR / "after.webp"
    Image.open(after_src).convert("RGB").save(a_png, "PNG")
    Image.open(after_src).convert("RGB").save(a_webp, "WEBP", quality=90, method=6)
    print(f"after: {a_png} ({a_png.stat().st_size}b), {a_webp} ({a_webp.stat().st_size}b)")
    # before
    b_png = IMG_DIR / "before.png"
    b_webp = IMG_DIR / "before.webp"
    Image.open(before_src).convert("RGB").save(b_png, "PNG")
    Image.open(before_src).convert("RGB").save(b_webp, "WEBP", quality=85, method=6)
    print(f"before: {b_png} ({b_png.stat().st_size}b), {b_webp} ({b_webp.stat().st_size}b)")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: install.py <after_src.png> <before_src.png>")
    install(sys.argv[1], sys.argv[2])
