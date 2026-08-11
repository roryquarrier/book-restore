#!/usr/bin/env python3
"""
Book Bash — Station 4 Worker
=============================
Polls the Supabase `jobs` table for uploaded books, runs them through the
production restore engine (~/book-restorer/restore.py), and pushes the
restored PDF back to Supabase Storage.

Job lifecycle driven by this worker:

    uploaded  --claim-->  extracting  --hold_credits()-->  regen_hold
              --restore.py running-->  regen_running
              --success-->  commit_credits()  ->  complete
              --failure-->  refund_credits()  ->  refunded  (+ error_message)

Credits: 1 per page, held up-front, committed on success, refunded on failure.
Hard cap: 80 pages per book.

Safety:
  - Atomic claim (conditional UPDATE) prevents two workers grabbing one job.
  - Heartbeat thread bumps updated_at every 30s while a job is in flight.
  - Dead-man switch reclaims jobs whose heartbeat went stale (>5 min),
    refunding any credits the dead worker had held.
  - SIGTERM/SIGINT finish the current job, then exit.

Run via start.sh (uses the book-restorer venv).
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz  # PyMuPDF — already a restore.py dependency, used for page counting
from dotenv import load_dotenv
from supabase import Client, create_client

# ── Paths & configuration ────────────────────────────────────────────────

WORKER_DIR = Path(__file__).resolve().parent
BOOK_BASH_DIR = WORKER_DIR.parent
RESTORER_DIR = Path(os.environ.get("RESTORER_DIR", Path.home() / "book-restorer"))
RESTORE_SCRIPT = RESTORER_DIR / "restore.py"

JOBS_ROOT = Path("/tmp/book-bash-jobs")
LOG_FILE = WORKER_DIR / "worker.log"

BUCKET = "book-bash"
CREDITS_PER_PAGE = 1
MAX_PAGES = 80

POLL_INTERVAL = 5           # seconds between queue polls
HEARTBEAT_INTERVAL = 30     # seconds between updated_at bumps
STALE_AFTER = 300           # seconds before an in-flight job is considered dead
RESTORE_TIMEOUT = 30 * 60   # 30 minutes

RESTORE_PROVIDER = os.environ.get("RESTORE_PROVIDER", "openai")
RESTORE_QUALITY = os.environ.get("RESTORE_QUALITY", "medium")

# Statuses that mean "a worker is supposedly working on this right now"
IN_FLIGHT_STATUSES = ["extracting", "regen_hold", "regen_running"]


# ── Logging: stdout + worker.log ─────────────────────────────────────────

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("worker")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()


# ── Environment ──────────────────────────────────────────────────────────

def load_config() -> tuple[str, str, str]:
    """Load Supabase + OpenAI credentials. Exits if anything essential is missing."""
    load_dotenv(BOOK_BASH_DIR / ".env")
    load_dotenv(RESTORER_DIR / ".env", override=False)

    url = os.environ.get("PUBLIC_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    missing = [
        name for name, value in (
            ("PUBLIC_SUPABASE_URL", url),
            ("SUPABASE_SERVICE_ROLE_KEY", service_key),
            ("OPENAI_API_KEY", openai_key),
        ) if not value
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    if not RESTORE_SCRIPT.exists():
        log.error("restore.py not found at %s", RESTORE_SCRIPT)
        sys.exit(1)

    return url, service_key, openai_key


# ── Graceful shutdown ────────────────────────────────────────────────────

shutdown = threading.Event()


def handle_signal(signum, _frame) -> None:
    name = signal.Signals(signum).name
    if shutdown.is_set():
        log.warning("%s received again — exiting immediately", name)
        os._exit(1)
    log.info("%s received — finishing current job, then shutting down", name)
    shutdown.set()


# ── Heartbeat ────────────────────────────────────────────────────────────

class Heartbeat:
    """Bumps jobs.updated_at on a timer so the dead-man switch stays quiet."""

    def __init__(self, sb: Client, job_id: str, interval: int = HEARTBEAT_INTERVAL):
        self.sb = sb
        self.job_id = job_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{job_id[:8]}", daemon=True
        )

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sb.table("jobs").update(
                    {"updated_at": now_iso()}
                ).eq("id", self.job_id).execute()
            except Exception as e:  # never let a heartbeat blip kill the job
                log.warning("[%s] heartbeat failed: %s", self.job_id[:8], e)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ── Helpers ──────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_pdf_pages(pdf_path: Path) -> int:
    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


class JobError(Exception):
    """A job-fatal error. Message is surfaced to the user via error_message."""


# ── Worker ───────────────────────────────────────────────────────────────

class Worker:
    def __init__(self, sb: Client, openai_key: str):
        self.sb = sb
        self.openai_key = openai_key

    # ── queue ────────────────────────────────────────────────────────────

    def reclaim_stale_jobs(self) -> None:
        """Dead-man switch: return jobs abandoned by a crashed worker to the queue.

        Any credits the dead worker held are refunded first, so the job is
        re-priced cleanly on its next run instead of double-holding.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER)).isoformat()
        try:
            stale = self.sb.table("jobs").select("id, status, credits_held") \
                .in_("status", IN_FLIGHT_STATUSES).lt("updated_at", cutoff) \
                .execute().data or []
        except Exception as e:
            log.warning("stale-job scan failed: %s", e)
            return

        for job in stale:
            job_id = job["id"]
            held = job.get("credits_held") or 0
            log.warning(
                "[%s] stale in '%s' for >%ds — returning to queue",
                job_id[:8], job["status"], STALE_AFTER,
            )
            try:
                if held > 0:
                    self.sb.rpc("refund_credits", {
                        "p_job_id": job_id, "p_amount": held
                    }).execute()
                    log.info("[%s] refunded %d stale held credits", job_id[:8], held)
                self.sb.table("jobs").update({
                    "status": "uploaded",
                    "credits_held": 0,
                    "updated_at": now_iso(),
                }).eq("id", job_id).execute()
            except Exception as e:
                log.error("[%s] failed to reclaim stale job: %s", job_id[:8], e)

    def claim_next_job(self) -> dict | None:
        """Atomically claim the oldest uploaded job. Returns None if the queue is empty."""
        candidates = self.sb.table("jobs") \
            .select("id, user_id, source_filename, storage_path") \
            .eq("status", "uploaded").order("created_at").limit(5) \
            .execute().data or []

        for job in candidates:
            # Conditional update — only one worker's UPDATE can match status='uploaded'.
            claimed = self.sb.table("jobs").update({
                "status": "extracting",
                "error_message": None,
                "updated_at": now_iso(),
            }).eq("id", job["id"]).eq("status", "uploaded").execute().data
            if claimed:
                return claimed[0]
            log.info("[%s] claimed by another worker — skipping", job["id"][:8])
        return None

    # ── job execution ────────────────────────────────────────────────────

    def process_job(self, job: dict) -> None:
        job_id = job["id"]
        short = job_id[:8]
        work_dir = JOBS_ROOT / job_id
        cost = 0
        held = False
        heartbeat = Heartbeat(self.sb, job_id).start()

        log.info("[%s] claimed: %s", short, job.get("source_filename"))

        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            input_pdf = self.download_source(job, work_dir)

            page_count = count_pdf_pages(input_pdf)
            log.info("[%s] %d pages", short, page_count)
            if page_count < 1:
                raise JobError("PDF contains no pages.")
            if page_count > MAX_PAGES:
                raise JobError(
                    f"Book is {page_count} pages — the limit is {MAX_PAGES} pages. "
                    "Please split it into smaller files."
                )

            cost = page_count * CREDITS_PER_PAGE
            self.sb.table("jobs").update({
                "page_count": page_count,
                "updated_at": now_iso(),
            }).eq("id", job_id).execute()

            # hold_credits() sets status='regen_hold' itself, and returns
            # false when the balance is short.
            ok = self.sb.rpc("hold_credits", {
                "p_job_id": job_id, "p_amount": cost
            }).execute().data
            if not ok:
                raise JobError(
                    f"Not enough credits: this book needs {cost} credits "
                    f"({page_count} pages x {CREDITS_PER_PAGE})."
                )
            held = True
            log.info("[%s] held %d credits", short, cost)

            self.sb.table("jobs").update({
                "status": "regen_running",
                "updated_at": now_iso(),
            }).eq("id", job_id).execute()

            output_pdf = self.run_restore(job_id, input_pdf, work_dir)

            storage_path = self.upload_output(job, output_pdf)
            log.info("[%s] uploaded -> %s", short, storage_path)

            self.sb.table("jobs").update({
                "output_storage_path": storage_path,
                "updated_at": now_iso(),
            }).eq("id", job_id).execute()

            # commit_credits() sets status='complete' and completed_at.
            self.sb.rpc("commit_credits", {
                "p_job_id": job_id, "p_amount": cost
            }).execute()
            log.info("[%s] COMPLETE — committed %d credits", short, cost)

        except Exception as e:
            message = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: {e}"
            log.error("[%s] FAILED: %s", short, message, exc_info=not isinstance(e, JobError))
            self.fail_job(job_id, message, cost if held else 0)

        finally:
            heartbeat.stop()
            self.cleanup(job_id, work_dir)

    def fail_job(self, job_id: str, message: str, held_credits: int) -> None:
        """Refund any hold, then record the terminal status and error message."""
        try:
            if held_credits > 0:
                # refund_credits() returns the credits and sets status='refunded'.
                self.sb.rpc("refund_credits", {
                    "p_job_id": job_id, "p_amount": held_credits
                }).execute()
                log.info("[%s] refunded %d credits", job_id[:8], held_credits)
                update = {"error_message": message, "updated_at": now_iso()}
            else:
                # Nothing was held, so no refund happened — 'failed' is the
                # honest terminal status.
                update = {
                    "status": "failed",
                    "error_message": message,
                    "updated_at": now_iso(),
                }
            self.sb.table("jobs").update(update).eq("id", job_id).execute()
        except Exception as e:
            log.error("[%s] could not record failure: %s", job_id[:8], e)

    # ── storage ──────────────────────────────────────────────────────────

    def download_source(self, job: dict, work_dir: Path) -> Path:
        storage_path = job["storage_path"]
        # Name the local file after the job id: restore.py derives its own
        # /tmp/restore_<stem> work directory from the filename, so a unique
        # stem keeps concurrent jobs from colliding.
        local = work_dir / f"{job['id']}.pdf"
        log.info("[%s] downloading %s", job["id"][:8], storage_path)
        data = self.sb.storage.from_(BUCKET).download(storage_path)
        local.write_bytes(data)
        if local.stat().st_size == 0:
            raise JobError("Downloaded source PDF is empty.")
        return local

    def upload_output(self, job: dict, output_pdf: Path) -> str:
        timestamp = int(time.time())
        storage_path = f"{job['user_id']}/{timestamp}_restored.pdf"
        self.sb.storage.from_(BUCKET).upload(
            storage_path,
            output_pdf.read_bytes(),
            {"content-type": "application/pdf", "upsert": "true"},
        )
        return storage_path

    # ── restore.py ───────────────────────────────────────────────────────

    def run_restore(self, job_id: str, input_pdf: Path, work_dir: Path) -> Path:
        short = job_id[:8]
        output_dir = work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        # restore.py's --output takes a PDF path, not a directory.
        output_pdf = output_dir / "restored.pdf"

        cmd = [
            sys.executable, str(RESTORE_SCRIPT), str(input_pdf),
            "--output", str(output_pdf),
            "--provider", RESTORE_PROVIDER,
            "--quality", RESTORE_QUALITY,
        ]
        env = {**os.environ, "OPENAI_API_KEY": self.openai_key}

        log.info("[%s] running restore.py (provider=%s quality=%s)",
                 short, RESTORE_PROVIDER, RESTORE_QUALITY)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=str(RESTORER_DIR), env=env,
                capture_output=True, text=True, timeout=RESTORE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise JobError(
                f"Restoration timed out after {RESTORE_TIMEOUT // 60} minutes."
            )

        elapsed = time.monotonic() - started
        for line in (proc.stdout or "").splitlines():
            log.info("[%s] restore | %s", short, line.rstrip())

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            log.error("[%s] restore.py exit %d:\n%s",
                      short, proc.returncode, "\n".join(tail))
            raise JobError(
                "Restoration failed: " + (tail[-1] if tail else "unknown error")
            )

        if not output_pdf.exists() or output_pdf.stat().st_size == 0:
            raise JobError("Restoration produced no output PDF.")

        log.info("[%s] restore.py finished in %.1f min (%.1f MB)",
                 short, elapsed / 60, output_pdf.stat().st_size / 1e6)
        return output_pdf

    # ── cleanup ──────────────────────────────────────────────────────────

    def cleanup(self, job_id: str, work_dir: Path) -> None:
        shutil.rmtree(work_dir, ignore_errors=True)
        # restore.py keeps its own scratch dir keyed on the input filename stem.
        shutil.rmtree(Path("/tmp") / f"restore_{job_id}", ignore_errors=True)

    # ── main loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("Worker started — polling every %ds (restore engine: %s)",
                 POLL_INTERVAL, RESTORE_SCRIPT)
        while not shutdown.is_set():
            try:
                self.reclaim_stale_jobs()
                job = self.claim_next_job()
                if job:
                    self.process_job(job)
                    continue  # check for more work straight away
            except Exception as e:
                log.error("poll loop error: %s", e, exc_info=True)
            shutdown.wait(POLL_INTERVAL)
        log.info("Worker stopped.")


def main() -> None:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    url, service_key, openai_key = load_config()
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)

    # Service role key — bypasses RLS so the worker can see every user's jobs.
    sb = create_client(url, service_key)
    Worker(sb, openai_key).run()


if __name__ == "__main__":
    main()
