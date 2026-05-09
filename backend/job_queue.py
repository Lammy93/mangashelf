import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import MANGA_DIR
from .db import get_db, db_conn
from .utils import logger

MAX_CONCURRENT = 3

_running_jobs = set()
_running_lock = threading.Lock()
_worker_started = False
_job_event = threading.Event()

def ensure_tables():
    with db_conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS download_jobs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                manga_title TEXT NOT NULL,
                chapter_number TEXT,
                chapter_id TEXT,
                status TEXT DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                error TEXT,
                priority INTEGER DEFAULT 0,
                output_format TEXT DEFAULT 'cbz',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
        """)
        db.commit()

ensure_tables()

def create_job(source: str, manga_title: str, chapter_number: str, chapter_id: str, output_format: str = "cbz") -> str:
    job_id = str(uuid.uuid4())
    with db_conn() as db:
        db.execute(
            "INSERT INTO download_jobs (id, source, manga_title, chapter_number, chapter_id, output_format, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, source, manga_title, chapter_number, chapter_id, output_format, 'queued', datetime.now().isoformat())
        )
        db.commit()
    _job_event.set()
    return job_id

def get_job(job_id: str) -> dict:
    with db_conn() as db:
        row = db.execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else {"status": "unknown"}

def list_jobs(limit: int = 50) -> list:
    with db_conn() as db:
        rows = db.execute("SELECT * FROM download_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def cancel_job(job_id: str):
    with db_conn() as db:
        db.execute("UPDATE download_jobs SET status='cancelled' WHERE id=? AND status IN ('queued','running')", (job_id,))
        db.commit()

def retry_job(job_id: str):
    with db_conn() as db:
        db.execute("UPDATE download_jobs SET status='queued', progress=0, error=NULL WHERE id=? AND status IN ('failed','cancelled')", (job_id,))
        db.commit()

def retry_all_failed():
    with db_conn() as db:
        db.execute("UPDATE download_jobs SET status='queued', progress=0, error=NULL WHERE status='failed'")
        db.commit()

def clear_completed(older_than_hours: int = 0):
    with db_conn() as db:
        if older_than_hours > 0:
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat()
            db.execute("DELETE FROM download_jobs WHERE status IN ('completed','cancelled') AND completed_at < ?", (cutoff,))
        else:
            db.execute("DELETE FROM download_jobs WHERE status IN ('completed','cancelled')")
        db.commit()

def update_job(job_id: str, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    with db_conn() as db:
        db.execute(f"UPDATE download_jobs SET {sets} WHERE id=?", vals)
        db.commit()

def _next_queued_job():
    with db_conn() as db:
        row = db.execute(
            "SELECT * FROM download_jobs WHERE status='queued' ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

def _worker_loop():
    while True:
        try:
            _job_event.wait()
            _job_event.clear()
            with _running_lock:
                if len(_running_jobs) >= MAX_CONCURRENT:
                    continue
            job = _next_queued_job()
            if not job:
                continue
            with _running_lock:
                _running_jobs.add(job["id"])
            update_job(job["id"], status="running", started_at=datetime.now().isoformat())
            threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        except Exception as e:
            logger.error(f"[job-queue] Worker error: {e}")
            time.sleep(2)

def _run_job(job: dict):
    try:
        from .services import _run_download_job
        _run_download_job(job)
    except Exception as e:
        logger.error(f"[job-queue] Job {job['id']} failed: {e}")
        update_job(job["id"], status="failed", error=str(e))
    finally:
        with _running_lock:
            _running_jobs.discard(job["id"])

def start_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        logger.info("[job-queue] Background worker started")
