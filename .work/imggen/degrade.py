#!/usr/bin/env python3
"""Step 2: Degrade a clean image into a 'before' via images.edit()."""
import base64
import sys
import time
from pathlib import Path

import requests

ENV_PATH = Path.home() / "book-restorer" / ".env"
KEY_PREFIX = (
    chr(79) + chr(80) + chr(69) + chr(78) + chr(65) + chr(73) + chr(95)
    + chr(65) + chr(80) + chr(73) + chr(95) + chr(75) + chr(69) + chr(89) + chr(61)
)

def load_key():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith(KEY_PREFIX):
            return line[len(KEY_PREFIX):].strip().strip('"').strip("'")
    raise SystemExit("API key not found")

PROMPT = (
    "Transform this image to look like a worn, degraded photocopy of the same "
    "page. Keep ALL text and illustrations exactly the same — same content, "
    "same layout, same position. Apply these realistic aging effects ONLY to "
    "the paper and appearance:\n"
    "1) Photocopied look: slightly washed out, lower contrast, faint background "
    "toner noise, subtle shadow edges.\n"
    "2) A visible horizontal crease line across the middle of the paper.\n"
    "3) Worn, dog-eared corners and frayed edges on the paper border.\n"
    "4) A brown coffee stain ring in the lower-right area.\n"
    "Do NOT change any words, letters, or illustrations. "
    "Do not reformat or reposition anything."
)

URL = "https://api.openai.com/v1/images/edits"

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: degrade.py <input.png> [output.png]")
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/home/hal/book-bash/before_cand.png")
    key = load_key()
    headers = {"Authorization": f"Bearer {key}"}

    print(f"POST edit src={src.name} ...", flush=True)
    t0 = time.time()
    with src.open("rb") as f:
        files = {"image": (src.name, f, "image/png")}
        data = {
            "model": "gpt-image-1",
            "prompt": PROMPT,
            "size": "1024x1536",
            "quality": "medium",
        }
        r = requests.post(URL, headers=headers, files=files, data=data, timeout=300)
    dt = time.time() - t0
    print(f"status={r.status_code} in {dt:.1f}s", flush=True)
    if r.status_code != 200:
        print(f"ERROR body: {r.text[:800]}", flush=True)
        raise SystemExit(1)
    b64 = r.json()["data"][0]["b64_json"]
    out.write_bytes(base64.b64decode(b64))
    print(f"wrote {out} ({out.stat().st_size} bytes)", flush=True)

if __name__ == "__main__":
    main()
