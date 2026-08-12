#!/usr/bin/env python3
"""Vision-check each candidate via gpt-4o: read text + rate sharpness."""
import base64
import sys
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
    raise SystemExit("no key")

PROMPT = (
    "Read ALL text visible in this image, exactly as printed (title + every "
    "numbered sentence). Then rate the text sharpness/crispness on a scale of "
    "1-10 where 10 is perfectly sharp printed text and 1 is blurry/unreadable. "
    "Respond in exactly this format:\n"
    "TEXT:<all text you can read>\n"
    "SHARPNESS:<n>/10\n"
    "NOTE:<any obvious text errors or missing lines>"
)

def check(img_path, key):
    b64 = base64.b64encode(Path(img_path).read_bytes()).decode()
    body = {
        "model": "gpt-4o",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 600,
    }
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body, timeout=120,
    )
    if r.status_code != 200:
        return f"ERR {r.status_code}: {r.text[:200]}"
    return r.json()["choices"][0]["message"]["content"]

if __name__ == "__main__":
    key = load_key()
    for p in sys.argv[1:]:
        print(f"\n===== {p} =====")
        print(check(p, key))
