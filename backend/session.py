import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

from .db import get_db, db_conn
from .utils import logger


def create_session(user_id: str, username: str, role: str, display_name: str = None, avatar: str = None) -> str:
    session_id = str(uuid.uuid4())
    data = json.dumps({"uid": user_id, "username": username, "role": role, "display_name": display_name, "avatar": avatar})
    with db_conn() as db:
        db.execute(
            "INSERT INTO sessions (id, user_id, data, created_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, data, datetime.now().isoformat())
        )
        # Clean old sessions for this user (keep last 5)
        db.execute("""
            DELETE FROM sessions WHERE user_id=? AND id NOT IN (
                SELECT id FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 5
            )
        """, (user_id, user_id))
        db.commit()
    return session_id

def verify_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    try:
        with db_conn() as db:
            row = db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row:
                return json.loads(row["data"])
    except Exception as e:
        logger.debug(f"[session] Verify error: {e}")
    return None

def delete_session(session_id: str):
    with db_conn() as db:
        db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        db.commit()

def rotate_session(session_id: str) -> str:
    data = verify_session(session_id)
    if not data:
        return None
    delete_session(session_id)
    return create_session(data["uid"], data["username"], data["role"], data.get("display_name"), data.get("avatar"))


# Periodic cleanup of expired sessions (>30 days)
_cleanup_started = False

def start_session_cleanup():
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    def _cleanup_loop():
        while True:
            threading.Event().wait(timeout=86400)
            cutoff = (datetime.now() - timedelta(days=30)).isoformat()
            with db_conn() as db:
                db.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
                db.commit()
            logger.debug("[session] Cleaned up expired sessions")
    threading.Thread(target=_cleanup_loop, daemon=True).start()
