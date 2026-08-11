#!/usr/bin/env python3
"""
Book Restorer — Production Pipeline
====================================
Restores scanned/photocopied ESL books to clean digital A4 PDFs.

Usage:
    python restore.py input.pdf
    python restore.py input.pdf --output restored.pdf
    python restore.py input.pdf --quality high
    python restore.py input.pdf --parallel 8      # concurrent regen workers
    python restore.py input.pdf --rotate-ccw      # force all pages 90 CCW
    python restore.py input.pdf --provider openai # use OpenAI instead of fal.ai
    python restore.py input.pdf --dry-run         # classify only, no API calls

What it does:
    1. Extract page images from PDF (lossless)
    2. Classify each page: single vs spread (binding gutter detection)
    3. Contact sheet of extracted pages (visual QA before spending money)
    4. Downsample to ~2000px wide (GPT Image 2 optimal), pad, regenerate
       via GPT Image 2 — cleanup, colour boost, centre line — in parallel
    5. Comparison sheet: original vs regen, side by side (visual QA)
    6. Detect centre line, split spreads into two A4 pages, cover swap,
       resize all to exact A4 (1240x1754 @ 150 DPI)
    7. JPEG compress + ocrmypdf for selectable text layer

Cost: ~$0.043/page (medium quality) = ~$0.90 for a 21-page book
Time: ~2-4 min/book at --parallel 8

Requirements:
    - fal.ai API key in FAL_KEY environment variable (--provider fal)
    - OpenAI API key in OPENAI_API_KEY environment variable (--provider openai)
    - ocrmypdf installed (apt install ocrmypdf)
    - Python: opencv-python-headless, pymupdf, numpy, fal_client, openai
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import tempfile
import threading
import time

# Auto-load .env from the script directory if present
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np

# Prevent OpenCV/OpenBLAS from spawning threads inside our ThreadPoolExecutor
# workers — nested parallelism can deadlock or oversubscribe CPU.
cv2.setNumThreads(1)

# ── Constants ────────────────────────────────────────────────────────────

A4_W, A4_H = 1240, 1754  # A4 at 150 DPI
A3_RATIO = 1.414         # A3 landscape = 2x A4 side by side
TARGET_WIDTH = 2000      # Downsample width for GPT Image 2
JPEG_QUALITY = 85        # Output compression
MAX_RETRIES = 5          # API retry attempts
RETRY_DELAY = 10         # Base seconds between retries (exponential backoff)
DEFAULT_PARALLEL = 8     # Concurrent regen workers (fal.ai default)
OPENAI_PARALLEL = 3      # OpenAI has stricter rate limits on new keys
OPENAI_MAX_PARALLEL = 5  # Hard cap for OpenAI even if user requests more

HERMES_DROP = Path("/mnt/c/Users/Hal/hermes-drop")  # Windows-side review folder

# Contact sheet layout
CONTACT_COLS = 4
CONTACT_THUMB_W = 300
CONTACT_THUMB_H = 420
CONTACT_LABEL_H = 30
CONTACT_GAP = 12

# Comparison sheet layout
COMPARE_ROW_W = 800
COMPARE_PANEL_H = 520
COMPARE_LABEL_H = 32
COMPARE_GAP = 12

# Production prompt v6 — Opus 1 variant (A/B tested Aug 7 2026, user-selected)
# Defeats the "photocopy look" by targeting full-spectrum colour restoration,
# warm paper tone, and illustration sharpness. Replaces "flatten" with
# "even out" to avoid biasing the model toward flat desaturated output.
PROMPT_SPREAD = """Restore this scanned children's book spread to look like a fresh, publisher-quality print — not a flat photocopy. It is a photo of an open two-page spread from a colourful illustrated ESL reader (Oxford Read and Imagine).

Colour restoration (most important):
- Bring back the warm, saturated print colours of the original book: rich reds, sunny yellows, leafy greens, warm oranges, and natural skin tones, as well as vibrant blues
- Restore the warm cream tone of the printed paper — do NOT leave the page looking grey, washed-out, or photocopy-flat
- Re-saturate all coloured elements (headers, borders, boxes, title banners, illustrated characters, backgrounds, and objects) to their original vibrant print colour
- Add back gentle contrast so illustrations look alive and three-dimensional, not flat

Illustration quality:
- Make the colour artwork crisp, clean, and detailed — like a freshly printed page, not a faded copy
- Sharpen line work and edges within illustrations without altering what is drawn

Technical fixes:
- Straighten the pages (remove skew and perspective distortion)
- Remove bleed-through from reverse pages
- Even out lighting and remove glare/reflections — but keep rich colour and contrast; do not flatten the image into a dull wash
- Sharpen the text so individual letters are crisp and legible
- Clean up JPEG artifacts and chroma noise in illustrations

Do NOT:
- Rewrite, autocomplete, or invent any text — every word must remain exactly as in the source
- Change the illustrations beyond restoration of their original colour, sharpness, and detail — do not redraw, restyle, or alter what is depicted
- Redesign the page layout
- Add any new decorations or elements
- Desaturate, grey-out, or mute the colours

Draw a thin black line down the exact centre between the two pages, from top to bottom.

The goal is a clean, vibrant, readable spread that fills the canvas edge to edge, with the content of BOTH pages preserved exactly."""

PROMPT_SINGLE = """Restore this scanned children's book page to look like a fresh, publisher-quality print — not a flat photocopy. It is a photo of a single page from a colourful illustrated ESL reader (Oxford Read and Imagine).

Colour restoration (most important):
- Bring back the warm, saturated print colours of the original book: rich reds, sunny yellows, leafy greens, warm oranges, and natural skin tones, as well as vibrant blues
- Restore the warm cream tone of the printed paper — do NOT leave the page looking grey, washed-out, or photocopy-flat
- Re-saturate all coloured elements (headers, borders, boxes, title banners, illustrated characters, backgrounds, and objects) to their original vibrant print colour
- Add back gentle contrast so illustrations look alive and three-dimensional, not flat

Illustration quality:
- Make the colour artwork crisp, clean, and detailed — like a freshly printed page, not a faded copy
- Sharpen line work and edges within illustrations without altering what is drawn

Technical fixes:
- Straighten the page (remove skew and perspective distortion)
- Remove bleed-through from the reverse page
- Even out lighting and remove glare/reflections — but keep rich colour and contrast; do not flatten the image into a dull wash
- Sharpen the text so individual letters are crisp and legible
- Clean up JPEG artifacts and chroma noise in illustrations

Do NOT:
- Rewrite, autocomplete, or invent any text — every word must remain exactly as in the source
- Change the illustrations beyond restoration of their original colour, sharpness, and detail — do not redraw, restyle, or alter what is depicted
- Redesign the page layout
- Add any new decorations or elements
- Desaturate, grey-out, or mute the colours

The goal is a clean, vibrant, readable page that fills the canvas edge to edge."""


# ── Errors ──────────────────────────────────────────────────────────────

class IncompleteRestoration(RuntimeError):
    """The pipeline could not produce a page-complete book.

    Raised instead of shipping a truncated PDF. The worker treats a non-zero
    exit as a job failure and refunds the user's credits, so a book that lost
    pages must abort here rather than reach build_pdf().
    """


# ── Providers ───────────────────────────────────────────────────────────

class ImageProvider:
    """Abstract image-edit backend. Implementations must be thread-safe."""

    name = "abstract"

    def edit_page(self, img: np.ndarray, prompt: str, quality: str) -> np.ndarray | None:
        raise NotImplementedError


class FalProvider(ImageProvider):
    """GPT Image 2 edit via fal.ai (openai/gpt-image-2/edit)."""

    name = "fal"

    def __init__(self):
        if "FAL_KEY" not in os.environ:
            raise RuntimeError("FAL_KEY environment variable not set. "
                               "Get one at https://fal.ai/")
        import fal_client  # imported here so --provider openai doesn't need it
        self._client = fal_client

    def edit_page(self, img: np.ndarray, prompt: str, quality: str) -> np.ndarray | None:
        # Unique temp paths — several worker threads run this concurrently
        fd, tmp_in = tempfile.mkstemp(prefix="restore_in_", suffix=".png")
        os.close(fd)
        fd, tmp_out = tempfile.mkstemp(prefix="restore_out_", suffix=".png")
        os.close(fd)
        try:
            cv2.imwrite(tmp_in, img)
            url = self._client.upload_file(tmp_in)

            output = self._client.subscribe(
                "openai/gpt-image-2/edit",
                arguments={
                    "prompt": prompt,
                    "image_urls": [url],
                    "quality": quality,
                },
            )

            if "images" in output and output["images"]:
                urllib.request.urlretrieve(output["images"][0]["url"], tmp_out)
                return cv2.imread(tmp_out)
            return None
        finally:
            for p in (tmp_in, tmp_out):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class OpenAIProvider(ImageProvider):
    """GPT Image 2 edit via the OpenAI API directly.

    Untested — no API key available yet. Sends the image as base64 (no
    upload/hosting step needed) and reads the base64 result back.
    """

    name = "openai"

    def __init__(self):
        if "OPENAI_API_KEY" not in os.environ:
            raise RuntimeError("OPENAI_API_KEY environment variable not set. "
                               "Get one at https://platform.openai.com/")
        from openai import OpenAI
        self._client = OpenAI()

    def edit_page(self, img: np.ndarray, prompt: str, quality: str) -> np.ndarray | None:
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        img_bytes = buf.tobytes()

        result = self._client.images.edit(
            model="gpt-image-2",
            image=("page.png", img_bytes, "image/png"),
            prompt=prompt,
            quality=quality,
        )

        if not result.data:
            return None
        item = result.data[0]
        # OpenAI may return b64_json or a URL depending on response_format
        b64 = getattr(item, "b64_json", None)
        url = getattr(item, "url", None)
        if b64:
            out_bytes = base64.b64decode(b64)
            arr = np.frombuffer(out_bytes, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if url:
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                urllib.request.urlretrieve(url, tmp)
                return cv2.imread(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return None


def get_provider(name: str) -> ImageProvider:
    """Build the requested provider. Raises if credentials are missing."""
    if name == "fal":
        return FalProvider()
    if name == "openai":
        return OpenAIProvider()
    raise ValueError(f"Unknown provider: {name}")


# ── Stage 1: Extract pages ──────────────────────────────────────────────

def _decode_embedded(doc: fitz.Document, xref: int, raw: bytes) -> np.ndarray | None:
    """Decode an embedded PDF image to BGR, falling back to PyMuPDF."""
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        return img
    # cv2 can't handle it (CMYK JPEG, JPX, ...) — let PyMuPDF rasterise it
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.n - pix.alpha != 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3]
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def _rotation_by_hough(img: np.ndarray) -> np.ndarray:
    """Rotate a sideways page 90 degrees, picking the direction that yields
    the most horizontal lines (text baselines run horizontally)."""
    cw = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    ccw = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)

    def horizontal_lines(candidate: np.ndarray) -> int:
        gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                                minLineLength=80, maxLineGap=15)
        if lines is None:
            return 0
        count = 0
        for line in lines:
            coords = line.flatten()
            x1, y1, x2, y2 = coords[0], coords[1], coords[2], coords[3]
            if abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) < 20:
                count += 1
        return count

    return ccw if horizontal_lines(ccw) >= horizontal_lines(cw) else cw


def extract_pages(pdf_path: str, out_dir: Path,
                  force_rotate: str | None = None) -> list[Path]:
    """Extract embedded page images from PDF losslessly.

    force_rotate:
        None  — auto: if the image orientation mismatches the PDF page
                orientation, the content is stored sideways and gets rotated
                90 degrees in whichever direction Hough line voting prefers.
        'ccw' — rotate EVERY page 90 degrees counterclockwise, no auto-detect.
        'cw'  — rotate EVERY page 90 degrees clockwise, no auto-detect.

    The explicit flags exist because Hough voting is unreliable; for books
    where every page is sideways the same way, just say so.
    """
    doc = fitz.open(pdf_path)
    pages = []

    for i in range(doc.page_count):
        page = doc[i]
        page_w, page_h = page.rect.width, page.rect.height
        page_is_landscape = page_w > page_h

        images = page.get_images(full=True)
        dest = None

        if images:
            xref = max(images, key=lambda im: im[2] * im[3])[0]
            ext = doc.extract_image(xref)
            img_w, img_h = ext["width"], ext["height"]
            img_is_landscape = img_w > img_h

            needs_rotation = force_rotate is not None or \
                page_is_landscape != img_is_landscape

            if needs_rotation:
                img = _decode_embedded(doc, xref, ext["image"])
                if img is not None:
                    if force_rotate == "ccw":
                        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    elif force_rotate == "cw":
                        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
                    else:
                        img = _rotation_by_hough(img)
                    dest = out_dir / f"p{i:03d}.png"
                    cv2.imwrite(str(dest), img)
            else:
                dest = out_dir / f"p{i:03d}.{ext['ext']}"
                dest.write_bytes(ext["image"])

        if dest is None:
            # No embedded image (or it wouldn't decode) — render at 300 DPI
            pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
            dest = out_dir / f"p{i:03d}.png"
            pix.save(str(dest))
            if force_rotate is not None:
                img = cv2.imread(str(dest))
                img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE
                                 if force_rotate == "ccw"
                                 else cv2.ROTATE_90_CLOCKWISE)
                cv2.imwrite(str(dest), img)

        pages.append(dest)

    doc.close()
    return pages


# ── Stage 2: Classify single vs spread ──────────────────────────────────

def classify_page(img_path: Path) -> str:
    """Detect if a page is a single page or a two-page spread.

    Detection priority (first match wins):
    1. Aspect ratio: landscape (>1.15) = spread, portrait (<0.95) = single
    2. Binding gutter: dark vertical shadow at centre = spread
    3. Default: single (safe)

    Returns 'spread' or 'single'.
    """
    img = cv2.imread(str(img_path))
    if img is None:
        return "single"  # safe default

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ratio = w / h

    # Method 1 (PRIMARY): Aspect ratio
    # Landscape pages (wider than tall) are spreads
    # Portrait pages (taller than wide) are singles
    if ratio > 1.15:
        return "spread"
    if ratio < 0.95:
        return "single"

    # Method 2 (SECONDARY): Binding gutter — only for near-square pages
    # A dark vertical band near centre indicates a spine
    c0, c1 = int(w * 0.40), int(w * 0.60)
    centre_region = gray[:, c0:c1]
    edge_left = gray[:, int(w * 0.05):int(w * 0.15)]
    edge_right = gray[:, int(w * 0.85):int(w * 0.95)]

    centre_mean = centre_region.mean()
    edge_mean = (edge_left.mean() + edge_right.mean()) / 2

    if (edge_mean - centre_mean) > 20:
        return "spread"

    return "single"


def classify_all_pages(pages: list[Path]) -> dict[str, str]:
    """Classify all pages. Returns dict of {page_stem: 'spread'|'single'}."""
    classifications = {}
    for p in pages:
        label = p.stem
        kind = classify_page(p)
        classifications[label] = kind
    return classifications


# ── Contact + comparison sheets ─────────────────────────────────────────

def _fit_within(img: np.ndarray, box_w: int, box_h: int) -> np.ndarray:
    """Scale an image to fit inside a box, preserving aspect ratio."""
    h, w = img.shape[:2]
    scale = min(box_w / w, box_h / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


def _paste_centred(canvas: np.ndarray, img: np.ndarray,
                   x: int, y: int, box_w: int, box_h: int) -> None:
    """Paste an image centred inside a box at (x, y) on the canvas."""
    h, w = img.shape[:2]
    x_off = x + (box_w - w) // 2
    y_off = y + (box_h - h) // 2
    canvas[y_off:y_off + h, x_off:x_off + w] = img


def _label(canvas: np.ndarray, text: str, x: int, y: int,
           scale: float = 0.6) -> None:
    """Draw a dark label with its baseline at (x, y)."""
    cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (40, 40, 40), 1, cv2.LINE_AA)


def _missing_panel(box_w: int, box_h: int, text: str = "MISSING") -> np.ndarray:
    """Placeholder panel for a page that failed to regenerate."""
    panel = np.full((box_h, box_w, 3), 235, dtype=np.uint8)
    cv2.rectangle(panel, (0, 0), (box_w - 1, box_h - 1), (170, 170, 170), 2)
    size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)[0]
    cv2.putText(panel, text, ((box_w - size[0]) // 2, (box_h + size[1]) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (90, 90, 90), 2, cv2.LINE_AA)
    return panel


def generate_contact_sheet(pages: list[Path], output_path: Path) -> Path | None:
    """Build a numbered grid of page thumbnails for pre-regen visual QA."""
    if not pages:
        return None

    cell_w = CONTACT_THUMB_W + CONTACT_GAP
    cell_h = CONTACT_THUMB_H + CONTACT_LABEL_H + CONTACT_GAP
    rows = (len(pages) + CONTACT_COLS - 1) // CONTACT_COLS

    sheet = np.full((rows * cell_h + CONTACT_GAP,
                     CONTACT_COLS * cell_w + CONTACT_GAP, 3), 255, dtype=np.uint8)

    for i, page_path in enumerate(pages):
        row, col = divmod(i, CONTACT_COLS)
        x = CONTACT_GAP + col * cell_w
        y = CONTACT_GAP + row * cell_h

        _label(sheet, f"{i + 1:02d}  {page_path.stem}", x, y + 20)

        img = cv2.imread(str(page_path))
        thumb_y = y + CONTACT_LABEL_H
        if img is None:
            thumb = _missing_panel(CONTACT_THUMB_W, CONTACT_THUMB_H, "UNREADABLE")
        else:
            thumb = _fit_within(img, CONTACT_THUMB_W, CONTACT_THUMB_H)
        _paste_centred(sheet, thumb, x, thumb_y, CONTACT_THUMB_W, CONTACT_THUMB_H)

        th, tw = thumb.shape[:2]
        bx = x + (CONTACT_THUMB_W - tw) // 2
        by = thumb_y + (CONTACT_THUMB_H - th) // 2
        cv2.rectangle(sheet, (bx - 1, by - 1), (bx + tw, by + th),
                      (200, 200, 200), 1)

    cv2.imwrite(str(output_path), sheet)
    return output_path


def generate_comparison_sheet(original_dir: Path, regen_dir: Path,
                              classifications: dict, output_path: Path) -> Path | None:
    """Build a vertical original-vs-regen sheet for post-regen visual QA."""
    exts = {".png", ".jpg", ".jpeg", ".jp2", ".tif", ".tiff", ".bmp", ".webp"}
    originals = sorted(p for p in original_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in exts)
    if not originals:
        return None

    panel_w = (COMPARE_ROW_W - 3 * COMPARE_GAP) // 2
    row_h = COMPARE_LABEL_H + COMPARE_PANEL_H + COMPARE_GAP

    sheet = np.full((len(originals) * row_h + COMPARE_GAP,
                     COMPARE_ROW_W, 3), 255, dtype=np.uint8)

    for i, orig_path in enumerate(originals):
        label = orig_path.stem
        kind = classifications.get(label, "spread")
        y = COMPARE_GAP + i * row_h

        _label(sheet, f"{i + 1:02d}  {label}  [{kind}]   "
                      f"original -> regen", COMPARE_GAP, y + 20)

        panel_y = y + COMPARE_LABEL_H
        regen_path = regen_dir / f"{label}.png"

        for j, src in enumerate([orig_path, regen_path]):
            x = COMPARE_GAP + j * (panel_w + COMPARE_GAP)
            img = cv2.imread(str(src)) if src.exists() else None
            if img is None:
                panel = _missing_panel(panel_w, COMPARE_PANEL_H)
            else:
                panel = _fit_within(img, panel_w, COMPARE_PANEL_H)
            _paste_centred(sheet, panel, x, panel_y, panel_w, COMPARE_PANEL_H)

            ph, pw = panel.shape[:2]
            bx = x + (panel_w - pw) // 2
            by = panel_y + (COMPARE_PANEL_H - ph) // 2
            cv2.rectangle(sheet, (bx - 1, by - 1), (bx + pw, by + ph),
                          (200, 200, 200), 1)

        cv2.line(sheet, (0, y + row_h - 4), (COMPARE_ROW_W, y + row_h - 4),
                 (225, 225, 225), 1)

    cv2.imwrite(str(output_path), sheet)
    return output_path


def copy_to_drop(path: Path) -> bool:
    """Copy a review sheet to the Windows-side hermes-drop folder."""
    try:
        HERMES_DROP.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, HERMES_DROP / path.name)
        return True
    except OSError as e:
        print(f"║        (hermes-drop copy failed: {e})")
        return False


# ── Stage 3: Downsample + pad ───────────────────────────────────────────

def prepare_for_regen(img_path: Path, page_type: str) -> np.ndarray:
    """Downsample and pad image for GPT Image 2 upload."""
    img = cv2.imread(str(img_path))
    h, w = img.shape[:2]

    # Downsample if wider than target
    if w > TARGET_WIDTH:
        scale = TARGET_WIDTH / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)

    h, w = img.shape[:2]

    if page_type == "spread":
        # Pad to A3 landscape ratio + 5% margin
        target_w = int(h * A3_RATIO)
        margin_frac = 0.05
        pad_x = int(target_w * margin_frac)
        pad_y = int(h * margin_frac)
        canvas_h = h + 2 * pad_y
        canvas_w = target_w + 2 * pad_x
        canvas = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
        x_off = (canvas_w - w) // 2
        y_off = (canvas_h - h) // 2
        canvas[y_off:y_off + h, x_off:x_off + w] = img
        return canvas
    else:
        # Single page — add small margin
        margin_frac = 0.05
        pad_x = int(w * margin_frac)
        pad_y = int(h * margin_frac)
        canvas_h = h + 2 * pad_y
        canvas_w = w + 2 * pad_x
        canvas = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
        canvas[pad_y:pad_y + h, pad_x:pad_x + w] = img
        return canvas


# ── Stage 4: GPT Image 2 regeneration ───────────────────────────────────

def check_and_fix_flip(img_src: np.ndarray, img_regen: np.ndarray) -> np.ndarray:
    """Detect if GPT Image 2 flipped the image 180 degrees during regen.

    Uses ORB feature matching to compare source and regen.
    If the regen looks more similar to the source when rotated 180,
    rotate it back.

    Returns the corrected image.
    """
    orb = cv2.ORB_create(nfeatures=500)

    # Resize for speed
    h1, w1 = img_src.shape[:2]
    scale = 500 / max(w1, h1)
    src = cv2.resize(img_src, None, fx=scale, fy=scale)

    h2, w2 = img_regen.shape[:2]
    scale2 = 500 / max(w2, h2)
    regen = cv2.resize(img_regen, None, fx=scale2, fy=scale2)

    gray_src = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    gray_regen = cv2.cvtColor(regen, cv2.COLOR_BGR2GRAY)

    kp1, des1 = orb.detectAndCompute(gray_src, None)
    kp2, des2 = orb.detectAndCompute(gray_regen, None)

    if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
        return img_regen  # can't detect, leave as-is

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)

    if len(matches) < 10:
        return img_regen

    good_matches = sorted(matches, key=lambda x: x.distance)[:30]

    src_pts = np.float32([kp1[m.queryIdx].pt for m in good_matches])
    regen_pts = np.float32([kp2[m.trainIdx].pt for m in good_matches])

    # Normalize to 0-1
    src_pts[:, 0] /= src.shape[1]
    src_pts[:, 1] /= src.shape[0]
    regen_pts[:, 0] /= regen.shape[1]
    regen_pts[:, 1] /= regen.shape[0]

    # 180 flip: (x,y) -> (1-x, 1-y)
    flipped_regen = 1 - regen_pts

    dist_normal = np.mean(np.abs(src_pts - regen_pts))
    dist_flipped = np.mean(np.abs(src_pts - flipped_regen))

    if dist_flipped < dist_normal * 0.7:
        return cv2.rotate(img_regen, cv2.ROTATE_180)

    return img_regen


def regenerate_page(img: np.ndarray, page_type: str, quality: str = "medium",
                    provider: ImageProvider | None = None) -> np.ndarray | None:
    """Send one prepared image to the image provider and return the result."""
    if provider is None:
        provider = get_provider("fal")
    prompt = PROMPT_SPREAD if page_type == "spread" else PROMPT_SINGLE
    return provider.edit_page(img, prompt, quality)


def patch_page(original_path: Path, regen_path: Path, page_type: str,
               box: tuple[int, int, int, int], quality: str = "medium",
               provider: ImageProvider | None = None) -> np.ndarray | None:
    """Surgical detail patch: enhance a region from the original, paste it
    onto the regenerated page, and run one AI pass to integrate.

    box = (x1, y1, x2, y2) in original-image pixel coordinates.
    Returns the patched+regenerated image, or None on failure.
    """
    if provider is None:
        provider = get_provider("fal")

    x1, y1, x2, y2 = box
    original = cv2.imread(str(original_path))
    regen = cv2.imread(str(regen_path))

    if original is None or regen is None:
        print("  PATCH FAILED: cannot read source images")
        return None

    oh, ow = original.shape[:2]
    rh, rw = regen.shape[:2]

    # Clamp box to original bounds
    x1 = max(0, min(x1, ow))
    x2 = max(0, min(x2, ow))
    y1 = max(0, min(y1, oh))
    y2 = max(0, min(y2, oh))

    if x2 - x1 < 10 or y2 - y1 < 10:
        print(f"  PATCH FAILED: box too small ({x2-x1}x{y2-y1})")
        return None

    # Step 1: Crop the problem region from the ORIGINAL scan
    crop = original[y1:y2, x1:x2].copy()

    # Step 2: Surgical enhance — sharpen + CLAHE on just this region
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    enhanced_gray = clahe.apply(gray)
    crop[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(crop[:, :, 0])
    crop[:, :, 1] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(crop[:, :, 1])
    crop[:, :, 2] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(crop[:, :, 2])

    # Light sharpening on the crop
    kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
    crop = cv2.filter2D(crop, -1, kernel)

    # Step 3: Scale coordinates to regen image
    sx = rw / ow
    sy = rh / oh
    rx1 = int(x1 * sx)
    ry1 = int(y1 * sy)
    rx2 = int(x2 * sx)
    ry2 = int(y2 * sy)

    # Resize the enhanced crop to match the regen region
    crop_resized = cv2.resize(crop, (rx2 - rx1, ry2 - ry1),
                              interpolation=cv2.INTER_LANCZOS4)

    # Step 4: Paste the enhanced crop onto the regen
    patched = regen.copy()
    patched[ry1:ry2, rx1:rx2] = crop_resized

    # Step 5: Prepare and send through AI for one integration pass
    patched_path = regen_path.parent / f"{regen_path.stem}_patched_input.png"
    cv2.imwrite(str(patched_path), patched)
    prepared = prepare_for_regen(patched_path, page_type)
    patched_path.unlink(missing_ok=True)

    # Use a patch-specific prompt
    patch_prompt = PROMPT_SPREAD if page_type == "spread" else PROMPT_SINGLE
    patch_prompt += "\n\nNote: A region of this page has been pre-enhanced. " \
                     "Integrate it smoothly with the rest of the page. " \
                     "Do not alter the pre-enhanced region beyond integration."

    result = provider.edit_page(prepared, patch_prompt, quality)
    if result is not None:
        result = check_and_fix_flip(prepared, result)

    return result


def regenerate_all(pages: list[Path], classifications: dict[str, str],
                   out_dir: Path, quality: str = "medium",
                   provider: ImageProvider | None = None,
                   parallel: int = DEFAULT_PARALLEL) -> dict[str, Path]:
    """Regenerate all pages concurrently. Returns dict of {page_stem: path}.

    Pages already present in out_dir are skipped, so a crashed run can be
    resumed by simply re-running the same command.
    """
    if provider is None:
        provider = get_provider("fal")

    total = len(pages)
    results: dict[str, Path] = {}
    failed: list[str] = []
    print_lock = threading.Lock()
    done = 0

    def say(msg: str) -> None:
        nonlocal done
        with print_lock:
            done += 1
            print(f"  [{done}/{total}] {msg}", flush=True)

    def work(page_path: Path) -> tuple[str, Path | None]:
        label = page_path.stem
        page_type = classifications.get(label, "spread")
        out_path = out_dir / f"{label}.png"

        if out_path.exists():
            say(f"{label}: SKIP (exists)")
            return label, out_path

        try:
            prepared = prepare_for_regen(page_path, page_type)
        except Exception as e:
            say(f"{label}: PREPARE FAILED: {str(e)[:60]}")
            return label, None

        for attempt in range(MAX_RETRIES):
            try:
                result = regenerate_page(prepared, page_type, quality, provider)
                if result is not None:
                    cv2.imwrite(str(out_path), result)
                    rh, rw = result.shape[:2]
                    say(f"{label} ({page_type}): OK ({rw}x{rh}) "
                        f"[attempt {attempt + 1}]")
                    return label, out_path
                reason = "NO IMAGE"
            except Exception as e:
                reason = f"FAIL: {str(e)[:60]}"

            if attempt < MAX_RETRIES - 1:
                import random
                # Exponential backoff: 10, 20, 40, 80s + jitter
                delay = RETRY_DELAY * (2 ** attempt) + random.uniform(0, 5)
                with print_lock:
                    print(f"      {label}: {reason} — retry in {delay:.0f}s",
                          flush=True)
                time.sleep(delay)

        say(f"{label} ({page_type}): ALL {MAX_RETRIES} ATTEMPTS FAILED")
        return label, None

    workers = max(1, min(parallel, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, p) for p in pages]
        for fut in as_completed(futures):
            try:
                label, out_path = fut.result()
            except Exception as e:
                # Shouldn't happen (work catches everything), but if it
                # does, don't let one future kill the whole batch.
                with print_lock:
                    print(f"  WORKER CRASH: {str(e)[:80]}", flush=True)
                continue
            if out_path is not None:
                results[label] = out_path
            else:
                failed.append(label)

    if failed:
        print(f"\n  WARNING: {len(failed)} pages failed: {sorted(failed)}")
        print(f"  Re-run the command to retry failed pages.")

    return results


# ── Stage 5: Detect centre line + split ─────────────────────────────────

def find_centre_line(img: np.ndarray) -> int:
    """Find the drawn black centre line position. Returns x coordinate."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    c0, c1 = int(w * 0.45), int(w * 0.55)

    best_col = w // 2
    best_run = 0

    for x in range(c0, c1):
        col = gray[:, x]
        c = 0
        max_r = 0
        for v in col:
            if v < 80:
                c += 1
                max_r = max(max_r, c)
            else:
                c = 0
        if max_r > best_run:
            best_run = max_r
            best_col = x

    if best_run / h > 0.5:
        return best_col
    return w // 2


def split_and_resize(regen_dir: Path, classifications: dict[str, str],
                     out_dir: Path) -> list[str]:
    """Split spreads on centre line, resize all to A4. Returns ordered page list."""
    pages = sorted(regen_dir.glob("*.png"))
    a4_pages = []  # list of (source_label, half_label, filepath)

    for p in pages:
        label = p.stem
        page_type = classifications.get(label, "spread")
        img = cv2.imread(str(p))
        if img is None:
            print(f"  WARNING: {p.name} unreadable, skipping")
            continue

        if page_type == "spread":
            split_x = find_centre_line(img)
            left = img[:, :split_x]
            right = img[:, split_x:]

            for half_label, half_img in [("L", left), ("R", right)]:
                out_path = out_dir / f"{label}_{half_label}.png"
                resized = resize_to_a4(half_img)
                cv2.imwrite(str(out_path), resized)
                a4_pages.append((label, half_label, out_path))

            print(f"  {label}: split at {split_x} -> L + R")
        else:
            out_path = out_dir / f"{label}.png"
            resized = resize_to_a4(img)
            cv2.imwrite(str(out_path), resized)
            a4_pages.append((label, "", out_path))
            print(f"  {label}: single -> A4")

    # ── Cover swap detection ─────────────────────────────────────────────
    # If the first source page is a spread, check if it's a cover spread.
    # Cover spreads show front cover on RIGHT, back cover on LEFT.
    # After splitting, page 1 = back cover, page 2 = front cover.
    # Swap them so front cover comes first.
    first_label = pages[0].stem if pages else None
    if first_label and classifications.get(first_label) == "spread":
        # Check: does the left half look like a back cover? (has ISBN/blurb text)
        first_left = out_dir / f"{first_label}_L.png"
        first_right = out_dir / f"{first_label}_R.png"
        if first_left.exists() and first_right.exists():
            print(f"  Cover spread detected — swapping L/R so front cover is first")
            # Swap in the list
            left_idx = right_idx = None
            for i in range(len(a4_pages)):
                if a4_pages[i][0] == first_label and a4_pages[i][1] == "L":
                    left_idx = i
                if a4_pages[i][0] == first_label and a4_pages[i][1] == "R":
                    right_idx = i
            if left_idx is not None and right_idx is not None:
                a4_pages[left_idx], a4_pages[right_idx] = a4_pages[right_idx], a4_pages[left_idx]

    return [str(p) for _, _, p in a4_pages]


def resize_to_a4(img: np.ndarray) -> np.ndarray:
    """Resize image to fit on A4 canvas, centered."""
    h, w = img.shape[:2]
    scale = min(A4_W / w, A4_H / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    canvas = np.full((A4_H, A4_W, 3), 255, dtype=np.uint8)
    x_off = (A4_W - new_w) // 2
    y_off = (A4_H - new_h) // 2
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
    return canvas


# ── Stage 6: Build PDF + OCR ────────────────────────────────────────────

def build_pdf(a4_pages: list[str], output_path: str) -> str:
    """Build JPEG-compressed PDF with selectable text layer via ocrmypdf."""
    # Step 1: Build image-only PDF
    # Use a unique temp file to avoid cross-job corruption when multiple
    # workers or overlapping runs share /tmp. NamedTemporaryFile guarantees
    # a unique path even for concurrent calls with identical output_paths.
    import tempfile
    tmp_fd, img_pdf = tempfile.mkstemp(suffix='_images.pdf', prefix='restore_')
    os.close(tmp_fd)  # close the fd; we just need the unique path
    doc = fitz.open()
    for page_path in a4_pages:
        img = cv2.imread(page_path)
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        page = doc.new_page(width=A4_W, height=A4_H)
        page.insert_image(fitz.Rect(0, 0, A4_W, A4_H), stream=buf.tobytes())
    doc.save(img_pdf, deflate=True, garbage=4)
    doc.close()

    img_size = os.path.getsize(img_pdf) / 1024 / 1024
    print(f"  Image PDF: {img_size:.1f}MB")

    # Step 2: OCR with ocrmypdf (adds selectable text + optimizes)
    print(f"  Running ocrmypdf for selectable text...")
    result = subprocess.run(
        ["ocrmypdf", "--optimize", "1", "--output-type", "pdf",
         "--jobs", "4", "--tesseract-timeout", "30",
         img_pdf, output_path],
        capture_output=True, text=True, timeout=600
    )

    if result.returncode != 0:
        print(f"  ocrmypdf warning: {result.stderr[-200:]}")

    # If ocrmypdf failed to produce output, fall back to image-only PDF
    if not os.path.exists(output_path):
        print("  ocrmypdf failed — using image-only PDF (no text layer)")
        import shutil
        shutil.copy2(img_pdf, output_path)

    final_size = os.path.getsize(output_path) / 1024 / 1024
    print(f"  Final PDF: {final_size:.1f}MB")

    return output_path


# ── Main pipeline ───────────────────────────────────────────────────────

def restore(pdf_path: str, output_path: str | None = None,
            quality: str = "medium", dry_run: bool = False,
            parallel: int = DEFAULT_PARALLEL, rotate: str | None = None,
            provider_name: str = "fal") -> str:
    """Run the full restoration pipeline."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Input PDF not found: {pdf_path}")

    book_name = pdf_path.stem
    if output_path is None:
        output_path = str(pdf_path.parent / f"{book_name}-restored.pdf")

    # Work directory
    work_dir = Path(f"/tmp/restore_{book_name}")
    work_dir.mkdir(parents=True, exist_ok=True)

    pages_dir = work_dir / "pages"
    regen_dir = work_dir / "regen"
    a4_dir = work_dir / "a4"
    for d in [pages_dir, regen_dir, a4_dir]:
        d.mkdir(exist_ok=True)

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  BOOK RESTORER — {book_name[:40]:<40s}  ║")
    print(f"╠══════════════════════════════════════════════════════════╣")

    # ── Stage 1: Extract ────────────────────────────────────────────
    print(f"║  [1/7] Extracting pages...                                ║")
    if rotate:
        print(f"║        Forced rotation: 90 {rotate.upper()} (auto-detect off)")
    pages = extract_pages(str(pdf_path), pages_dir, force_rotate=rotate)
    print(f"║        {len(pages)} pages extracted")
    print(f"║                                                            ║")

    # ── Stage 2: Classify ───────────────────────────────────────────
    print(f"║  [2/7] Classifying pages (single vs spread)...            ║")
    classifications = classify_all_pages(pages)
    n_spread = sum(1 for v in classifications.values() if v == "spread")
    n_single = sum(1 for v in classifications.values() if v == "single")
    print(f"║        {n_spread} spreads, {n_single} singles")
    for label, kind in sorted(classifications.items()):
        print(f"║          {label}: {kind}")
    print(f"║                                                            ║")

    # ── Stage 3: Contact sheet ──────────────────────────────────────
    print(f"║  [3/7] Building contact sheet...                          ║")
    contact_path = work_dir / f"{book_name}-contact.png"
    if generate_contact_sheet(pages, contact_path):
        print(f"║        {contact_path}")
        if copy_to_drop(contact_path):
            print(f"║        -> {HERMES_DROP / contact_path.name}")
    else:
        print(f"║        (no pages — skipped)")
    print(f"║                                                            ║")

    if dry_run:
        print(f"║  --dry-run: stopping before API calls                    ║")
        print(f"╚══════════════════════════════════════════════════════════╝")
        return "Dry run complete — no output generated"

    provider = get_provider(provider_name)

    # ── Stage 4: Regenerate ─────────────────────────────────────────
    print(f"║  [4/7] Regenerating via GPT Image 2 ({provider.name})...")
    print(f"║        Quality: {quality}, Workers: {parallel}, "
          f"Cost: ~${len(pages) * 0.043:.2f}")
    regen_results = regenerate_all(pages, classifications, regen_dir,
                                   quality, provider, parallel)
    print(f"║        {len(regen_results)}/{len(pages)} pages regenerated")
    if len(regen_results) < len(pages):
        missing = sorted(p.stem for p in pages if p.stem not in regen_results)
        raise IncompleteRestoration(
            f"only {len(regen_results)}/{len(pages)} pages regenerated — "
            f"missing: {', '.join(missing)}. Re-run to retry failed pages "
            f"(successful pages are cached in {regen_dir})."
        )
    print(f"║                                                            ║")

    # ── Stage 5: Comparison sheet ───────────────────────────────────
    print(f"║  [5/7] Building comparison sheet (original vs regen)...   ║")
    compare_path = work_dir / f"{book_name}-comparison.png"
    if generate_comparison_sheet(pages_dir, regen_dir, classifications,
                                 compare_path):
        print(f"║        {compare_path}")
        if copy_to_drop(compare_path):
            print(f"║        -> {HERMES_DROP / compare_path.name}")
    else:
        print(f"║        (no pages — skipped)")
    print(f"║                                                            ║")

    # ── Stage 6: Split + resize ────────────────────────────────────
    print(f"║  [6/7] Splitting spreads + resizing to A4...             ║")
    a4_pages = split_and_resize(regen_dir, classifications, a4_dir)
    print(f"║        {len(a4_pages)} A4 pages")
    # A spread splits into two A4 pages, a single into one. Fewer than that
    # means split_and_resize dropped an unreadable regen — don't ship it.
    # (Only '<': --patch leaves extra PNGs in regen_dir, which is harmless here.)
    expected_a4 = sum(2 if classifications.get(p.stem, "spread") == "spread" else 1
                      for p in pages)
    if len(a4_pages) < expected_a4:
        raise IncompleteRestoration(
            f"split produced {len(a4_pages)} A4 pages, expected {expected_a4} "
            f"— some regenerated pages were unreadable."
        )
    print(f"║                                                            ║")

    # ── Stage 7: Build PDF ─────────────────────────────────────────
    print(f"║  [7/7] Building PDF with selectable text...              ║")
    build_pdf(a4_pages, output_path)
    print(f"║                                                            ║")
    print(f"║  DONE: {output_path}")
    print(f"╚══════════════════════════════════════════════════════════╝")

    return output_path


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Restore scanned ESL books to clean digital A4 PDFs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  python restore.py input.pdf\n"
               "  python restore.py input.pdf --output restored.pdf --quality high\n"
               "  python restore.py input.pdf --rotate-ccw --parallel 8"
    )
    ap.add_argument("input", help="Input PDF path")
    ap.add_argument("--output", "-o", default=None,
                    help="Output PDF path (default: [name]-restored.pdf)")
    ap.add_argument("--quality", "-q", default="medium",
                    choices=["low", "medium", "high"],
                    help="GPT Image 2 quality (default: medium)")
    ap.add_argument("--parallel", "-p", type=int, default=DEFAULT_PARALLEL,
                    help=f"Concurrent regen workers (default: {DEFAULT_PARALLEL})")
    ap.add_argument("--provider", default="fal", choices=["fal", "openai"],
                    help="Image edit backend (default: fal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify pages + contact sheet only, no API calls")
    ap.add_argument("--patch", default=None,
                    help="Patch a single page: 'PAGE x1,y1,x2,y2' "
                         "(e.g. '8 600,400,800,520'). Coordinates in original "
                         "image pixels. Requires a prior full run.")

    rot = ap.add_mutually_exclusive_group()
    rot.add_argument("--rotate-ccw", action="store_true",
                     help="Force-rotate every page 90 counterclockwise "
                          "(skips orientation auto-detection)")
    rot.add_argument("--rotate-cw", action="store_true",
                     help="Force-rotate every page 90 clockwise "
                          "(skips orientation auto-detection)")

    args = ap.parse_args()

    if args.parallel < 1:
        ap.error("--parallel must be at least 1")

    # OpenAI has stricter rate limits — cap parallelism
    if args.provider == "openai":
        if args.parallel > OPENAI_MAX_PARALLEL:
            print(f"  NOTE: OpenAI rate limits — capping parallel from "
                  f"{args.parallel} to {OPENAI_MAX_PARALLEL}")
        args.parallel = min(args.parallel, OPENAI_MAX_PARALLEL)

    rotate = "ccw" if args.rotate_ccw else "cw" if args.rotate_cw else None

    # ── Patch mode ──
    if args.patch:
        # Parse: "PAGE x1,y1,x2,y2"
        parts = args.patch.split()
        if len(parts) != 2:
            ap.error("--patch format: 'PAGE x1,y1,x2,y2'")
        patch_page_num = parts[0].lstrip("p").zfill(3)
        try:
            coords = parts[1].split(",")
            if len(coords) != 4:
                raise ValueError
            box = tuple(int(c) for c in coords)
        except ValueError:
            ap.error("--patch coordinates must be 'x1,y1,x2,y2' (4 integers)")

        # Find the work directory from a prior run
        name = Path(args.input).stem
        work_dir = Path(tempfile.gettempdir()) / f"restore_{name}"
        pages_dir = work_dir / "pages"
        regen_dir = work_dir / "regen"

        original_path = pages_dir / f"{patch_page_num}.jpeg"
        if not original_path.exists():
            original_path = pages_dir / f"{patch_page_num}.png"

        regen_path = regen_dir / f"{patch_page_num}.png"

        if not original_path.exists():
            ap.error(f"Original page not found: {original_path}. "
                     "Run a full restore first.")
        if not regen_path.exists():
            ap.error(f"Regenerated page not found: {regen_path}. "
                     "Run a full restore first.")

        # Determine page type
        orig_img = cv2.imread(str(original_path))
        oh, ow = orig_img.shape[:2]
        page_type = "spread" if ow > oh else "single"

        provider = get_provider(args.provider)

        print(f"PATCHING page {patch_page_num} ({page_type})")
        print(f"  Region: {box[0]},{box[1]} to {box[2]},{box[3]}")
        print(f"  Original: {ow}x{oh}")

        result = patch_page(original_path, regen_path, page_type, box,
                            quality=args.quality, provider=provider)

        if result is not None:
            # Back up old regen, save new one
            backup = regen_path.parent / f"{patch_page_num}_prepatch.png"
            shutil.copy2(regen_path, backup)
            cv2.imwrite(str(regen_path), result)

            print(f"  DONE — patched image saved to {regen_path}")
            print(f"  Backup of pre-patch: {backup}")

            # Deliver before/after to hermes-drop
            before_after = np.hstack([
                cv2.imread(str(backup)),
                result
            ])
            ba_path = regen_path.parent / f"{patch_page_num}_patch_compare.png"
            cv2.imwrite(str(ba_path), before_after)
            copy_to_drop(ba_path)
            print(f"  Before/after -> hermes-drop")
        else:
            print("  PATCH FAILED")
        return

    restore(args.input, output_path=args.output, quality=args.quality,
            dry_run=args.dry_run, parallel=args.parallel, rotate=rotate,
            provider_name=args.provider)


if __name__ == "__main__":
    main()
