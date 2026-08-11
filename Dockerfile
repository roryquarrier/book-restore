# ── Build stage: install Python deps ──────────────────────────────
FROM python:3.13-slim AS base

# System deps needed by OpenCV (libGL etc.), PyMuPDF, and ocrmypdf (tesseract + ghostscript + pngquant)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    tesseract-ocr \
    ghostscript \
    pngquant \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching)
# opencv-python-headless + pymupdf + numpy = restore.py engine
# supabase + python-dotenv = worker queue client
RUN pip install --no-cache-dir \
    "openai>=2.0" \
    "opencv-python-headless>=4.10" \
    "pymupdf>=1.24" \
    "numpy>=1.26" \
    "pillow>=10.0" \
    "supabase>=2.10,<3" \
    "python-dotenv>=1.0" \
    "ocrmypdf>=16.0"

# Copy worker + restore engine
COPY worker/worker.py worker/restore.py ./

# Worker polls Supabase — no inbound ports needed
# But we declare one for health checks
EXPOSE 8080

# Graceful shutdown: SIGTERM finishes current job
STOPSIGNAL SIGTERM

CMD ["python3", "-u", "worker.py"]
