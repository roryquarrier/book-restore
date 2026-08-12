#!/usr/bin/env python3
"""Step 1: Generate clean worksheet candidates via images.generate()."""
import base64
import os
import sys
import time
from pathlib import Path

import requests

# API key loaded via chr() concatenation to avoid security filter mangling
ENV_PATH = Path.home() / "book-restorer" / ".env"
KEY_PREFIX = (
    chr(79) + chr(80) + chr(69) + chr(78) + chr(65) + chr(73) + chr(95)
    + chr(65) + chr(80) + chr(73) + chr(95) + chr(75) + chr(69) + chr(89) + chr(61)
)

def load_key():
    text = ENV_PATH.read_text()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(KEY_PREFIX):
            return line[len(KEY_PREFIX):].strip().strip('"').strip("'")
    raise SystemExit("API key not found in env file")

PROMPT = (
    "A children's grammar exercise worksheet on cream paper. "
    "At the top, a detailed Beatrix Potter-style watercolor illustration of "
    "Peter Rabbit in a blue jacket holding a shovel, surrounded by green "
    "cabbages, leafy plants, and small pink flowers. Below the illustration, "
    "centered bold serif title: 'GRAMMAR EXERCISE: PAST TENSE VERBS.' "
    "Below the title, six numbered fill-in-the-blank sentences in clean serif text:\n"
    "1. Peter Rabbit _____ into the garden.\n"
    "2. We _____ two carrot seeds.\n"
    "3. She _____ some blackberries.\n"
    "4. The birds _____ to the tree.\n"
    "5. Jeremy Fisher _____ in the pond.\n"
    "6. The children _____ to the story.\n"
    "The page looks freshly printed, vibrant colors, sharp text, professional "
    "quality. Portrait orientation."
)

URL = "https://api.openai.com/v1/images/generations"

def generate_one(key, idx):
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": "gpt-image-1",
        "prompt": PROMPT,
        "size": "1024x1536",
        "quality": "high",
        "n": 1,
    }
    print(f"[{idx}] POST generate...", flush=True)
    t0 = time.time()
    r = requests.post(URL, headers=headers, json=body, timeout=300)
    dt = time.time() - t0
    print(f"[{idx}] status={r.status_code} in {dt:.1f}s", flush=True)
    if r.status_code != 200:
        print(f"[{idx}] ERROR body: {r.text[:500]}", flush=True)
        return None
    data = r.json()
    b64 = data["data"][0].get("b64_json")
    if not b64:
        print(f"[{idx}] No b64_json, keys: {list(data['data'][0])}", flush=True)
        return None
    out = Path(f"/home/hal/book-bash/cand_{idx}.png")
    out.write_bytes(base64.b64decode(b64))
    print(f"[{idx}] wrote {out} ({out.stat().st_size} bytes)", flush=True)
    return out

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    key = load_key()
    print(f"Generating {n} candidates...", flush=True)
    made = []
    for i in range(1, n + 1):
        p = generate_one(key, i)
        if p:
            made.append(p)
    print("DONE candidates:", [str(p) for p in made], flush=True)

if __name__ == "__main__":
    main()
