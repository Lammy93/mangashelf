import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from passlib.context import CryptContext

from .config import SUPPORTED_IMAGE_EXTS

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

logger = logging.getLogger("mangashelf")

def hash_password(pw):
    return pwd_context.hash(pw)

def verify_password(pw, hashed):
    return pwd_context.verify(pw, hashed)

def generate_id() -> str:
    return str(uuid.uuid4())

# Background task queue
_background_queue = queue.Queue()

def _background_worker():
    while True:
        task = _background_queue.get()
        if task is None:
            break
        fn, args = task
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"[bg] Task failed: {e}")
        time.sleep(3)
        _background_queue.task_done()

threading.Thread(target=_background_worker, daemon=True).start()

def bg_submit(fn, *args):
    _background_queue.put((fn, args))

# Processing tracking
_processing_manga = set()
_processing_lock = threading.Lock()
_extracting_chapters = set()
_extracting_chapters_lock = threading.Lock()

def is_manga_processing(manga_id: str) -> bool:
    with _processing_lock:
        return manga_id in _processing_manga

def mark_manga_processing(manga_id: str, processing: bool):
    with _processing_lock:
        if processing:
            _processing_manga.add(manga_id)
        else:
            _processing_manga.discard(manga_id)

# Scan progress tracking
_scan_progress = {"running": False, "total": 0, "current": 0, "new_manga": [], "message": ""}

def get_scan_progress():
    return dict(_scan_progress)

def set_scan_progress(**kwargs):
    _scan_progress.update(kwargs)

# Rate limiter
_login_attempts = {}
_login_lock = threading.Lock()

def check_login_rate_limit(ip: str) -> bool:
    with _login_lock:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        if len(attempts) >= 10:
            return False
        attempts.append(now)
        _login_attempts[ip] = attempts
        return True
