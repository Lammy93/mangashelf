import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
import fitz
import zipfile
import rarfile
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import CACHE_DIR, SUPPORTED_FORMATS, SUPPORTED_IMAGE_EXTS
from .db import get_db, get_scan_setting, set_scan_setting, get_all_manga_directories, slugify
from .models import (
    SourceAdd, ReadingProgress, ReadingModeUpdate, MangaDirAdd,
    ScanSettingsUpdate, UserSettingsUpdate, UserCreate, NfoUpdate, DownloadRequest
)
from .session import verify_session, create_session
from .nfo import read_nfo, write_nfo
from .cache import _metadata_cache
from .utils import (
    logger, hash_password, verify_password, generate_id, is_manga_processing,
    get_scan_progress, set_scan_progress
)
from .services import (
    scan_manga_dir, extract_pages, extract_chapter_bg, start_folder_watcher,
    download_mangadex_chapter, download_mangasee_chapter, download_batoto_chapter,
    download_asurascans_chapter, download_comick_chapter, download_flamescans_chapter,
    search_mangadex, search_anilist, search_myanimelist,
    search_mangasee, search_batoto, search_asurascans, search_comick, search_flamescans,
    get_mangasee_chapters, get_batoto_chapters, get_asurascans_chapters,
    get_comick_chapters, get_flamescans_chapters,
    auto_fetch_metadata, extract_metadata, pre_extract_pages,
    _run_download_job, search_mangal_all
)
from .job_queue import create_job, get_job, list_jobs, cancel_job, start_worker

router = APIRouter()

# ── Auth helpers ────────────────────────────────────────────────────────

def require_auth(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = verify_session(token)
    if not user:
        raise HTTPException(401, "Invalid session")
    return user

def require_admin(request: Request):
    user = require_auth(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user

# ── Library ─────────────────────────────────────────────────────────────

@router.get("/api/library")
async def get_library(q: str = ""):
    db = get_db()
    if q:
        rows = db.execute("""
            SELECT m.*, COUNT(c.id) as downloaded_chapters
            FROM manga m
            LEFT JOIN chapters c ON c.manga_id = m.id
            WHERE m.title LIKE ?
            GROUP BY m.id
            ORDER BY m.title
        """, (f"%{q}%",)).fetchall()
    else:
        rows = db.execute("""
            SELECT m.*, COUNT(c.id) as downloaded_chapters
            FROM manga m
            LEFT JOIN chapters c ON c.manga_id = m.id
            GROUP BY m.id
            ORDER BY m.title
        """).fetchall()
    db.close()
    result = [dict(r) for r in rows]
    for item in result:
        item["processing"] = is_manga_processing(item["id"])
    return result

@router.get("/api/continue-reading")
async def get_continue_reading():
    db = get_db()
    rows = db.execute("""
        SELECT m.id, m.slug, m.title, m.cover, c.id as chapter_id, c.slug as chapter_slug,
               c.chapter_number, c.title as chapter_title, c.read_page as page, c.pages
        FROM manga m
        JOIN chapters c ON c.manga_id = m.id
        WHERE c.read_page > 0 AND c.is_read = 0
        ORDER BY m.updated_at DESC
        LIMIT 10
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/api/recently-added")
async def get_recently_added():
    db = get_db()
    rows = db.execute("""
        SELECT m.*, COUNT(c.id) as downloaded_chapters
        FROM manga m
        LEFT JOIN chapters c ON c.manga_id = m.id
        GROUP BY m.id
        ORDER BY m.added_at DESC
        LIMIT 8
    """).fetchall()
    db.close()
    result = [dict(r) for r in rows]
    for item in result:
        item["processing"] = is_manga_processing(item["id"])
    return result

# ── Scan Settings ───────────────────────────────────────────────────────

@router.get("/api/scan-settings")
async def get_scan_settings(request: Request):
    require_admin(request)
    return {
        "scan_interval": int(get_scan_setting("scan_interval") or 300),
        "auto_scan_enabled": get_scan_setting("auto_scan_enabled") == "1",
        "watch_enabled": get_scan_setting("watch_enabled") == "1",
        "scan_on_folder_change": get_scan_setting("scan_on_folder_change") == "1",
        "directories": get_all_manga_directories()
    }

@router.post("/api/scan-settings")
async def update_scan_settings(data: ScanSettingsUpdate, request: Request):
    require_admin(request)
    if data.scan_interval is not None:
        set_scan_setting("scan_interval", str(max(300, data.scan_interval)))
    if data.auto_scan_enabled is not None:
        set_scan_setting("auto_scan_enabled", "1" if data.auto_scan_enabled else "0")
    if data.watch_enabled is not None:
        set_scan_setting("watch_enabled", "1" if data.watch_enabled else "0")
        start_folder_watcher()
    if data.scan_on_folder_change is not None:
        set_scan_setting("scan_on_folder_change", "1" if data.scan_on_folder_change else "0")
    return get_scan_settings(request)

@router.get("/api/scan-directories")
async def get_scan_directories(request: Request):
    require_admin(request)
    return get_all_manga_directories()

@router.post("/api/scan-directories")
async def add_scan_directory(data: MangaDirAdd, request: Request):
    require_admin(request)
    p = Path(data.path)
    if not p.exists():
        raise HTTPException(400, "Directory does not exist.")
    db = get_db()
    existing = db.execute("SELECT id FROM manga_directories WHERE path=?", (data.path,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(409, "Directory already added.")
    db.execute(
        "INSERT INTO manga_directories (id, path, enabled, added_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), data.path, 1, datetime.now().isoformat())
    )
    db.commit()
    db.close()
    start_folder_watcher()
    scan_manga_dir()
    return {"ok": True}

@router.delete("/api/scan-directories/{dir_id}")
async def delete_scan_directory(dir_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM manga_directories WHERE id=?", (dir_id,))
    db.commit()
    db.close()
    start_folder_watcher()
    return {"ok": True}

@router.patch("/api/scan-directories/{dir_id}/toggle")
async def toggle_scan_directory(dir_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE manga_directories SET enabled = 1 - enabled WHERE id=?", (dir_id,))
    db.commit()
    db.close()
    start_folder_watcher()
    scan_manga_dir()
    return {"ok": True}

# ── Favorites ───────────────────────────────────────────────────────────

@router.post("/api/manga/{manga_id}/favorite")
async def toggle_favorite(manga_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    existing = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id)).fetchone()
    if existing:
        db.execute("DELETE FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id))
        db.commit(); db.close()
        return {"ok": True, "favorited": False}
    db.execute("INSERT OR IGNORE INTO favorites (user_id, manga_id, added_at) VALUES (?,?,?)", (user["uid"], manga_id, datetime.now().isoformat()))
    db.commit(); db.close()
    return {"ok": True, "favorited": True}

@router.get("/api/favorites")
async def get_favorites(request: Request):
    user = require_auth(request)
    db = get_db()
    rows = db.execute("""
        SELECT m.*, COUNT(c.id) as downloaded_chapters
        FROM manga m
        JOIN favorites f ON f.manga_id = m.id
        LEFT JOIN chapters c ON c.manga_id = m.id
        WHERE f.user_id=?
        GROUP BY m.id
        ORDER BY f.added_at DESC
    """, (user["uid"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/api/manga/{manga_id}/is-favorite")
async def is_favorite(manga_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    existing = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id)).fetchone()
    db.close()
    return {"favorited": existing is not None}

# ── Followed Manga ──────────────────────────────────────────────────────

@router.post("/api/follow")
async def follow_manga(request: Request):
    user = require_auth(request)
    data = await request.json()
    manga_id = str(uuid.uuid4())
    db = get_db()
    existing = db.execute("SELECT id FROM followed_manga WHERE user_id=? AND source=? AND source_id=?", (user["uid"], data.get("source",""), data.get("source_id",""))).fetchone()
    if existing:
        db.close()
        return {"ok": True, "followed": True}
    db.execute("INSERT INTO followed_manga (id, user_id, source, source_id, title, cover, last_chapter_count, added_at) VALUES (?,?,?,?,?,?,?,?)", (
        manga_id, user["uid"], data.get("source",""), data.get("source_id",""), data.get("title",""), data.get("cover",""), data.get("chapters", 0), datetime.now().isoformat()
    ))
    db.commit(); db.close()
    return {"ok": True, "followed": True}

@router.post("/api/unfollow/{follow_id}")
async def unfollow_manga(follow_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    db.execute("DELETE FROM followed_manga WHERE id=? AND user_id=?", (follow_id, user["uid"]))
    db.commit(); db.close()
    return {"ok": True}

@router.get("/api/followed")
async def get_followed(request: Request):
    user = require_auth(request)
    db = get_db()
    rows = db.execute("SELECT * FROM followed_manga WHERE user_id=? ORDER BY added_at DESC", (user["uid"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("/api/check-followed-updates")
async def check_followed_updates(request: Request):
    user = require_auth(request)
    db = get_db()
    followed = db.execute("SELECT * FROM followed_manga WHERE user_id=?", (user["uid"],)).fetchall()
    updates = []
    for item in followed:
        source = item["source"]
        source_id = item["source_id"]
        new_count = item["last_chapter_count"]
        if source == "mangadex":
            async with aiohttp.ClientSession() as session:
                url = f"https://api.mangadex.org/manga/{source_id}/feed?translatedLanguage[]=en&order[chapter]=desc&limit=1"
                async with session.get(url) as resp:
                    ch_data = await resp.json()
            new_count = len(ch_data.get("data", []))
        if new_count > item["last_chapter_count"]:
            db.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
            updates.append({"title": item["title"], "new_chapters": new_count - item["last_chapter_count"]})
    db.commit()
    db.close()
    return {"ok": True, "updates": updates}

# ── Scan Actions ────────────────────────────────────────────────────────

@router.post("/api/scan-now")
async def scan_now(request: Request):
    require_admin(request)
    if get_scan_progress()["running"]:
        return {"ok": False, "message": "Scan already running"}
    threading.Thread(target=scan_manga_dir, daemon=True).start()
    return {"ok": True}

@router.get("/api/scan-progress")
async def get_scan_progress_endpoint(request: Request):
    require_admin(request)
    return get_scan_progress()

@router.post("/api/scrape-all-covers")
async def scrape_all_covers(request: Request):
    require_admin(request)
    def scrape_covers():
        db = get_db()
        rows = db.execute("SELECT id, title, path FROM manga WHERE cover IS NULL OR cover=''").fetchall()
        for row in rows:
            manga_path = Path(row["path"])
            files = []
            if manga_path.is_dir():
                files = [f for f in sorted(manga_path.iterdir()) if f.suffix.lower() in SUPPORTED_FORMATS]
            elif manga_path.suffix.lower() in SUPPORTED_FORMATS:
                files = [manga_path]
            if files:
                try:
                    pages = extract_pages(files[0])
                    if pages:
                        db.execute("UPDATE manga SET cover=? WHERE id=?", (pages[0], row["id"]))
                except Exception as e:
                    logger.warning(f"[covers] Scrape failed for {row.get('title','')}: {e}")
        db.commit(); db.close()
    threading.Thread(target=scrape_covers, daemon=True).start()
    return {"ok": True}

# ── User Settings ───────────────────────────────────────────────────────

@router.get("/api/me")
async def get_me(request: Request):
    user = require_auth(request)
    db = get_db()
    u = db.execute("SELECT id, username, role, display_name, avatar, created_at FROM users WHERE id=?", (user["uid"],)).fetchone()
    settings = db.execute("SELECT * FROM user_settings WHERE user_id=?", (user["uid"],)).fetchone()
    db.close()
    result = dict(u) if u else {}
    if settings:
        result["settings"] = dict(settings)
    else:
        result["settings"] = {"reading_mode": "single", "strip_scroll_sensitivity": 1.0, "auto_hide_toolbar": 1, "show_page_numbers": 1}
    return result

@router.post("/api/me")
async def update_me(data: UserSettingsUpdate, request: Request):
    user = require_auth(request)
    db = get_db()
    u = db.execute("SELECT display_name, avatar FROM users WHERE id=?", (user["uid"],)).fetchone()
    if data.display_name is not None:
        db.execute("UPDATE users SET display_name=? WHERE id=?", (data.display_name, user["uid"]))
        u = db.execute("SELECT display_name, avatar FROM users WHERE id=?", (user["uid"],)).fetchone()
    if data.reading_mode is not None or data.strip_scroll_sensitivity is not None or data.auto_hide_toolbar is not None or data.show_page_numbers is not None:
        db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, reading_mode, strip_scroll_sensitivity, auto_hide_toolbar, show_page_numbers) SELECT ?, COALESCE(?, reading_mode), COALESCE(?, strip_scroll_sensitivity), COALESCE(?, auto_hide_toolbar), COALESCE(?, show_page_numbers) FROM user_settings WHERE user_id=?",
            (user["uid"], data.reading_mode, data.strip_scroll_sensitivity, int(data.auto_hide_toolbar) if data.auto_hide_toolbar is not None else None, int(data.show_page_numbers) if data.show_page_numbers is not None else None, user["uid"])
        )
    db.commit(); db.close()
    new_session = create_session(user["uid"], user["username"], user["role"], u["display_name"] if u else None, u["avatar"] if u else None)
    response = JSONResponse({"ok": True})
    response.set_cookie(key="session", value=new_session, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@router.post("/api/me/avatar")
async def upload_avatar(request: Request):
    user = require_auth(request)
    form = await request.form()
    avatar_file = form.get("avatar")
    if not avatar_file or not hasattr(avatar_file, 'read'):
        raise HTTPException(400, "No file uploaded.")
    content = await avatar_file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 5MB).")
    avatar_dir = Path("/data/avatars")
    avatar_dir.mkdir(parents=True, exist_ok=True)
    fname = getattr(avatar_file, 'filename', '')
    ext = Path(fname).suffix.lower() if fname else '.png'
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        ext = '.png'
    avatar_path = avatar_dir / f"{user['uid']}{ext}"
    with open(avatar_path, "wb") as f:
        f.write(content)
    db = get_db()
    avatar_url = f"/avatars/{user['uid']}{ext}"
    db.execute("UPDATE users SET avatar=? WHERE id=?", (avatar_url, user["uid"]))
    db.commit()
    u = db.execute("SELECT display_name, avatar FROM users WHERE id=?", (user["uid"],)).fetchone()
    db.close()
    new_session = create_session(user["uid"], user["username"], user["role"], u["display_name"] if u else None, u["avatar"] if u else None)
    response = JSONResponse({"ok": True, "avatar": avatar_url})
    response.set_cookie(key="session", value=new_session, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

# ── Manga Detail ────────────────────────────────────────────────────────

@router.get("/api/manga/{manga_id}")
async def get_manga(manga_id: str, request: Request):
    require_auth(request)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        db.close()
        raise HTTPException(404, "Manga not found")
    volumes = db.execute("SELECT * FROM volumes WHERE manga_id=? ORDER BY volume_number", (manga_id,)).fetchall()
    chapters = db.execute("SELECT id, manga_id, chapter_number, title, slug, path, pages, read_page, is_read, source_url, downloaded FROM chapters WHERE manga_id=? ORDER BY chapter_number", (manga_id,)).fetchall()
    db.close()
    progress = {r["id"]: {"is_read": r["is_read"], "page": r["read_page"], "pages": r["pages"]} for r in chapters}
    return {"manga": dict(manga), "volumes": [dict(v) for v in volumes], "chapters": [dict(c) for c in chapters], "progress": progress}

@router.post("/api/manga/{manga_id}/scrape-cover")
async def scrape_manga_cover(manga_id: str, request: Request):
    require_admin(request)
    def do_scrape():
        db = get_db()
        manga = db.execute("SELECT path FROM manga WHERE id=?", (manga_id,)).fetchone()
        if manga:
            manga_path = Path(manga["path"])
            files = []
            if manga_path.is_dir():
                files = [f for f in sorted(manga_path.iterdir()) if f.suffix.lower() in SUPPORTED_FORMATS]
            elif manga_path.suffix.lower() in SUPPORTED_FORMATS:
                files = [manga_path]
            if files:
                try:
                    pages = extract_pages(files[0])
                    if pages:
                        db.execute("UPDATE manga SET cover=? WHERE id=?", (pages[0], manga_id))
                        db.commit()
                except Exception as e:
                    logger.warning(f"[cover] Scrape failed for manga {manga_id}: {e}")
        db.close()
    threading.Thread(target=do_scrape, daemon=True).start()
    return {"ok": True}

@router.post("/api/manga/{manga_id}/fetch-metadata")
async def fetch_metadata(manga_id: str, request: Request):
    require_admin(request)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")
    threading.Thread(target=auto_fetch_metadata, args=(manga_id, manga["path"], manga["title"]), daemon=True).start()
    return {"ok": True, "message": f"Fetching metadata for '{manga['title']}'"}

# ── Reading ─────────────────────────────────────────────────────────────

@router.post("/api/progress")
async def save_progress(data: ReadingProgress):
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (data.chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404)
    db.execute("UPDATE chapters SET read_page=? WHERE id=?", (data.page, data.chapter_id))
    db.execute("UPDATE manga SET last_read_chapter=?, last_read_page=?, updated_at=? WHERE id=?",
               (ch["chapter_number"], data.page, datetime.now().isoformat(), ch["manga_id"]))
    if ch["pages"] > 0 and data.page >= ch["pages"] - 1:
        db.execute("UPDATE chapters SET is_read=1 WHERE id=?", (data.chapter_id,))
    db.commit(); db.close()
    return {"ok": True}

@router.post("/api/manga/{manga_id}/reading-mode")
async def set_reading_mode(manga_id: str, data: ReadingModeUpdate):
    db = get_db()
    db.execute("UPDATE manga SET reading_mode=? WHERE id=?", (data.mode, manga_id))
    db.commit(); db.close()
    return {"ok": True}

# ── Chapter Pages ───────────────────────────────────────────────────────

@router.get("/api/chapter/{chapter_id}/pages")
async def get_pages(chapter_id: str):
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404, "Chapter not found")
    file_path = Path(ch["path"])
    out_dir = CACHE_DIR / file_path.stem
    if out_dir.exists():
        pages = sorted([f for f in out_dir.iterdir() if f.suffix in SUPPORTED_IMAGE_EXTS])
        if pages:
            urls = [f"/cache/{file_path.stem}/{p.name}" for p in pages]
            db.execute("UPDATE chapters SET pages=? WHERE id=?", (len(pages), chapter_id))
            db.commit(); db.close()
            return {"pages": urls, "total": len(pages)}
    suffix = file_path.suffix.lower()
    total = 0
    try:
        if suffix in ('.cbz', '.zip'):
            with zipfile.ZipFile(file_path) as z:
                total = len([n for n in z.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
        elif suffix in ('.cbr', '.rar'):
            with rarfile.RarFile(file_path) as r:
                total = len([n for n in r.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
        elif suffix == '.pdf':
            doc = fitz.open(str(file_path))
            total = len(doc)
            doc.close()
    except Exception as e:
        logger.warning(f"[pages] Error counting pages in {file_path.name}: {e}")
    db.execute("UPDATE chapters SET pages=? WHERE id=?", (total, chapter_id))
    db.commit(); db.close()
    urls = [f"/api/chapter/{chapter_id}/page/{i}" for i in range(total)]
    from .utils import _extracting_chapters, _extracting_chapters_lock
    stem = file_path.stem
    with _extracting_chapters_lock:
        if stem not in _extracting_chapters:
            _extracting_chapters.add(stem)
            threading.Thread(target=extract_chapter_bg, args=(str(file_path),), daemon=True).start()
    return {"pages": urls, "total": total}

@router.get("/api/chapter/{chapter_id}/page/{page_num}")
async def get_chapter_page(chapter_id: str, page_num: int):
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404, "Chapter not found")
    file_path = Path(ch["path"])
    out_dir = CACHE_DIR / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = file_path.suffix.lower()
    ok = False
    try:
        if suffix in ('.cbz', '.zip'):
            with zipfile.ZipFile(file_path) as z:
                imgs = sorted([n for n in z.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
                if page_num >= len(imgs):
                    raise HTTPException(404, "Page not found")
                img_member = imgs[page_num]
                img_name = Path(img_member).name
                cached = out_dir / img_name
                if not cached.exists():
                    cached.write_bytes(z.read(img_member))
                ok = True
                return FileResponse(str(cached))
        elif suffix in ('.cbr', '.rar'):
            with rarfile.RarFile(file_path) as r:
                imgs = sorted([n for n in r.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
                if page_num >= len(imgs):
                    raise HTTPException(404, "Page not found")
                img_member = imgs[page_num]
                img_name = Path(img_member).name
                cached = out_dir / img_name
                if not cached.exists():
                    cached.write_bytes(r.read(img_member))
                ok = True
                return FileResponse(str(cached))
        elif suffix == '.pdf':
            cached = out_dir / f"page_{page_num:04d}.png"
            if not cached.exists():
                doc = fitz.open(str(file_path))
                if page_num >= len(doc):
                    doc.close()
                    raise HTTPException(404, "Page not found")
                pix = doc[page_num].get_pixmap(dpi=150)
                pix.save(str(cached))
                doc.close()
            ok = True
            return FileResponse(str(cached))
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[page] Error extracting page {page_num} from {file_path.name}: {e}")
        raise HTTPException(500, "Failed to extract page")
    finally:
        if ok:
            from .utils import _extracting_chapters, _extracting_chapters_lock
            stem = file_path.stem
            with _extracting_chapters_lock:
                if stem not in _extracting_chapters:
                    _extracting_chapters.add(stem)
                    threading.Thread(target=extract_chapter_bg, args=(str(file_path),), daemon=True).start()
        db.close()
    raise HTTPException(400, "Unsupported file format")

# ── Users (Admin) ───────────────────────────────────────────────────────

@router.get("/api/users")
async def get_users(request: Request):
    require_admin(request)
    db = get_db()
    rows = db.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at").fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("/api/users")
async def create_user(data: UserCreate, request: Request):
    require_admin(request)
    if len(data.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters.")
    if len(data.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if data.role not in ("user", "admin"):
        raise HTTPException(400, "Invalid role.")
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username=?", (data.username,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(409, "Username already exists.")
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), data.username, hash_password(data.password), data.role, datetime.now().isoformat())
    )
    db.commit(); db.close()
    return {"ok": True}

@router.delete("/api/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    user = require_admin(request)
    if user["uid"] == user_id:
        raise HTTPException(400, "Cannot delete yourself.")
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit(); db.close()
    return {"ok": True}

# ── Sources ─────────────────────────────────────────────────────────────

@router.get("/api/sources")
async def get_sources():
    db = get_db()
    rows = db.execute("SELECT * FROM sources ORDER BY name").fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("/api/sources")
async def add_source(data: SourceAdd):
    db = get_db()
    src_id = str(uuid.uuid4())
    db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",
               (src_id, data.name, data.base_url, 'custom', 1, datetime.now().isoformat()))
    db.commit(); db.close()
    return {"id": src_id, "name": data.name, "base_url": data.base_url}

@router.delete("/api/sources/{source_id}")
async def delete_source(source_id: str):
    db = get_db()
    db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    db.commit(); db.close()
    return {"ok": True}

@router.patch("/api/sources/{source_id}/toggle")
async def toggle_source(source_id: str):
    db = get_db()
    db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    db.commit(); db.close()
    return {"ok": True}

# ── Search ──────────────────────────────────────────────────────────────

@router.get("/api/search")
async def search_manga(q: str, source: str = "mangadex"):
    if source == "mangadex":
        return await search_mangadex(q)
    if source == "anilist":
        return await search_anilist(q)
    if source == "myanimelist":
        return await search_myanimelist(q)
    if source == "mangakakalot":
        async with aiohttp.ClientSession() as session:
            url = f"https://mangakakalot.com/search/story/{q.replace(' ', '_')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("div.daily-update-storie-item")[:20]:
            link = item.select_one("h3 a")
            img = item.select_one("img")
            if link and img:
                title = link.get("title", "") or link.text.strip()
                href = link.get("href", "")
                cover = img.get("src", "")
                if not cover.startswith("http"):
                    cover = f"https://mangakakalot.com{cover}"
                results.append({"id": href, "title": title, "cover": cover, "status": None, "source": "mangakakalot", "description": ""})
        return results
    if source == "mangafox":
        async with aiohttp.ClientSession() as session:
            url = f"https://fanfox.net/search?title={q}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select(".search-result-item")[:20]:
            link = item.select_one("a")
            img = item.select_one("img")
            if link:
                title = link.get("title", "") or link.text.strip()
                href = link.get("href", "")
                cover = img.get("src", "") if img else ""
                if cover and not cover.startswith("http"):
                    cover = f"https://fanfox.net{cover}"
                results.append({"id": href, "title": title, "cover": cover, "status": None, "source": "mangafox", "description": ""})
        return results
    if source == "mangasee":
        return await search_mangasee(q)
    if source == "batoto":
        return await search_batoto(q)
    if source == "asurascans":
        return await search_asurascans(q)
    if source == "comick":
        return await search_comick(q)
    if source == "flamescans":
        return await search_flamescans(q)
    return []

@router.get("/api/search-all")
async def search_all_sources(q: str):
    async def search_source(source_id, source_type):
        try:
            if source_type == "mangadex":
                return await search_mangadex(q, limit=5)
            if source_type == "anilist":
                return await search_anilist(q, limit=5)
            if source_type == "myanimelist":
                return await search_myanimelist(q, limit=5)
            if source_type == "mangasee":
                return await search_mangasee(q, limit=5)
            if source_type == "batoto":
                return await search_batoto(q, limit=5)
            if source_type == "asurascans":
                return await search_asurascans(q, limit=5)
            if source_type == "comick":
                return await search_comick(q, limit=5)
            if source_type == "flamescans":
                return await search_flamescans(q, limit=5)
            if source_type == "mangal":
                return await search_mangal_all(q, limit=5)
            return []
        except Exception as e:
            logger.debug(f"[search-all] Source search failed: {e}")
            return []
    db = get_db()
    rows = db.execute("SELECT id, type FROM sources WHERE enabled=1 AND type IN ('mangadex','mangasee','batoto','asurascans','comick','flamescans','mangal','anilist','myanimelist')").fetchall()
    db.close()
    tasks = [search_source(r["id"], r["type"]) for r in rows]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    merged = []
    for result in all_results:
        if isinstance(result, list):
            merged.extend(result)
    return merged

# ── Source Chapters ─────────────────────────────────────────────────────

@router.get("/api/manga-source/{source}/{manga_id}/chapters")
async def get_source_chapters(source: str, manga_id: str):
    if source == "mangadex":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga/{manga_id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100"
            async with session.get(url) as resp:
                data = await resp.json()
        chapters = []
        for ch in data.get("data", []):
            attr = ch["attributes"]
            chapters.append({
                "id": ch["id"], "chapter": attr.get("chapter"),
                "title": attr.get("title") or f"Chapter {attr.get('chapter','')}",
                "pages": attr.get("pages", 0), "source": "mangadex"
            })
        return chapters
    if source == "mangakakalot":
        async with aiohttp.ClientSession() as session:
            url = f"https://mangakakalot.com{manga_id}" if not manga_id.startswith("http") else manga_id
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for row in soup.select("ul.row-content-chapter li")[:500]:
            link = row.select_one("a")
            if link:
                ch_text = link.text.strip()
                import re
                m = re.search(r'chapter\s*([\d.]+)', ch_text, re.I)
                ch_num = m.group(1) if m else ch_text
                chapters.append({"id": link.get("href", ""), "chapter": ch_num, "title": ch_text, "pages": 0, "source": "mangakakalot"})
        chapters.reverse()
        return chapters
    if source == "mangafox":
        async with aiohttp.ClientSession() as session:
            url = f"https://fanfox.net{manga_id}" if not manga_id.startswith("http") else manga_id
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for row in soup.select(".detail-main-list li")[:500]:
            link = row.select_one("a")
            if link:
                ch_text = link.select_one("span").text.strip() if link.select_one("span") else link.text.strip()
                import re
                m = re.search(r'ch\.?([\d.]+)', ch_text, re.I)
                ch_num = m.group(1) if m else ch_text
                chapters.append({"id": link.get("href", ""), "chapter": ch_num, "title": ch_text, "pages": 0, "source": "mangafox"})
        return chapters
    if source == "myanimelist":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jikan.moe/v4/manga/{manga_id}"
            async with session.get(url) as resp:
                data = await resp.json()
        m = data.get("data", {})
        return [{"id": "info", "chapter": "1", "title": f"{m.get('title', '')} — Ch.1–{m.get('chapters', '?')}", "pages": 0, "source": "myanimelist",
                 "metadata": {"score": m.get("score"), "scored_by": m.get("scored_by"), "rank": m.get("rank"), "popularity": m.get("popularity"),
                              "members": m.get("members"), "favorites": m.get("favorites"), "volumes": m.get("volumes"), "chapters": m.get("chapters"),
                              "status": m.get("status"), "published": m.get("published", {}).get("string"),
                              "authors": [a["name"] for a in m.get("authors", [])], "genres": [g["name"] for g in m.get("genres", [])],
                              "themes": [t["name"] for t in m.get("themes", [])], "serialization": [s["name"] for s in m.get("serialization", [])]}}]
    if source == "anilist":
        return []
    if source == "mangasee":
        return await get_mangasee_chapters(manga_id)
    if source == "batoto":
        return await get_batoto_chapters(manga_id)
    if source == "asurascans":
        return await get_asurascans_chapters(manga_id)
    if source == "comick":
        return await get_comick_chapters(manga_id)
    if source == "flamescans":
        return await get_flamescans_chapters(manga_id)
    if source.startswith("mangal_"):
        from .mangal_source import get_mangal_chapters
        mangal_src = source.split("mangal_", 1)[1]
        return await get_mangal_chapters(mangal_src, manga_id)
    return []

# ── Metadata ────────────────────────────────────────────────────────────

@router.get("/api/manga/{manga_id}/metadata")
async def get_external_metadata(manga_id: str, source: str = "anilist", external_id: str = "", cover: str = ""):
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        db.close()
        raise HTTPException(404, "Manga not found")

    if source == "anilist" and external_id:
        query = """
        query($id: Int) {
          Media(id: $id, type: MANGA) {
            id title { romaji english native } coverImage { large medium }
            status description(asHtml: false) chapters volumes genres synonyms format
            startDate { year month day } endDate { year month day }
            staff { edges { role node { name { full } } } }
            studios { edges { isMain node { name } } }
          }
        }"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"id": int(external_id)}}
            ) as resp:
                data = await resp.json()
        m = data.get("data", {}).get("Media", {})
        if m:
            title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or ""
            authors = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", []) if e.get("role") and ("Story" in e["role"] or "Art" in e["role"])]
            ext_cover = cover or m.get("coverImage", {}).get("large") or m.get("coverImage", {}).get("medium")
            db.execute(
                "UPDATE manga SET title=?, author=?, artist=?, genre=?, summary=?, status=?, total_chapters=?, year=?, cover=?, updated_at=? WHERE id=?",
                (title, authors[0] if authors else manga["author"], authors[1] if len(authors) > 1 else manga["artist"],
                 ", ".join(m.get("genres", [])), (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
                 m.get("status", "").upper() if m.get("status") else manga["status"], m.get("chapters") or manga["total_chapters"],
                 m.get("startDate", {}).get("year") or manga["year"], ext_cover, datetime.now().isoformat(), manga_id)
            )
            db.commit()
            manga_path = Path(manga["path"])
            if manga_path.is_dir():
                write_nfo(manga_path, {"Title": title, "Author": authors[0] if authors else manga["author"],
                    "Artist": authors[1] if len(authors) > 1 else manga["artist"], "Genre": ", ".join(m.get("genres", [])),
                    "Summary": (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
                    "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
                    "TotalChapters": m.get("chapters") or manga["total_chapters"], "Year": m.get("startDate", {}).get("year") or manga["year"],
                    "Cover": ext_cover, "Source": "anilist", "SourceId": external_id})
                _metadata_cache.invalidate(manga_id)
        db.close()
        return {"ok": True, "data": m} if m else {"ok": False, "error": "Not found"}

    if source == "myanimelist" and external_id:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jikan.moe/v4/manga/{external_id}/full"
            async with session.get(url) as resp:
                data = await resp.json()
        m = data.get("data", {})
        if m:
            authors = [a["name"] for a in m.get("authors", [])]
            genres = [g["name"] for g in m.get("genres", [])]
            ext_cover = cover or m.get("images", {}).get("jpg", {}).get("large_image_url") or m.get("images", {}).get("jpg", {}).get("image_url")
            db.execute(
                "UPDATE manga SET title=?, author=?, artist=?, genre=?, summary=?, status=?, total_chapters=?, year=?, publisher=?, cover=?, updated_at=? WHERE id=?",
                (m.get("title") or m.get("title_english") or manga["title"], authors[0] if authors else manga["author"],
                 authors[1] if len(authors) > 1 else manga["artist"], ", ".join(genres),
                 (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
                 m.get("status", "").upper() if m.get("status") else manga["status"], m.get("chapters") or manga["total_chapters"],
                 m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
                 (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
                 ext_cover, datetime.now().isoformat(), manga_id)
            )
            db.commit()
            manga_path = Path(manga["path"])
            if manga_path.is_dir():
                write_nfo(manga_path, {"Title": m.get("title") or m.get("title_english") or manga["title"],
                    "Author": authors[0] if authors else manga["author"], "Artist": authors[1] if len(authors) > 1 else manga["artist"],
                    "Genre": ", ".join(genres), "Summary": (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
                    "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
                    "TotalChapters": m.get("chapters") or manga["total_chapters"],
                    "Year": m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
                    "Publisher": (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
                    "Cover": ext_cover, "Source": "myanimelist", "SourceId": external_id})
                _metadata_cache.invalidate(manga_id)
        db.close()
        return {"ok": True, "data": m} if m else {"ok": False, "error": "Not found"}

    db.close()
    raise HTTPException(400, "Invalid source or missing external_id")

@router.post("/api/manga/{manga_id}/rematch")
async def rematch_manga(manga_id: str, request: Request):
    require_admin(request)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")
    data = await request.json()
    source = data.get("source")
    external_id = data.get("external_id")
    cover_url = data.get("cover_url")
    title_override = data.get("title")

    if source == "anilist" and external_id:
        return await _apply_anilist_rematch(manga_id, manga, external_id, cover_url)
    elif source == "myanimelist" and external_id:
        return await _apply_mal_rematch(manga_id, manga, external_id, cover_url)
    else:
        return await _apply_simple_rematch(manga_id, manga, source, external_id, title_override, cover_url)


async def _apply_anilist_rematch(manga_id, manga, external_id, cover_url):
    query = """
    query($id: Int) {
      Media(id: $id, type: MANGA) {
        id title { romaji english native } coverImage { large medium }
        status description(asHtml: false) chapters volumes genres
        startDate { year month day }
        staff { edges { role node { name { full } } } }
      }
    }"""
    async with aiohttp.ClientSession() as session:
        async with session.post("https://graphql.anilist.co",
            json={"query": query, "variables": {"id": int(external_id)}}) as resp:
            result = await resp.json()
    m = result.get("data", {}).get("Media", {})
    if not m:
        raise HTTPException(404, "Match not found on AniList")
    title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or ""
    authors = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", [])
               if e.get("role") and ("Story" in e["role"] or "Art" in e["role"])]
    ext_cover = cover_url or m.get("coverImage", {}).get("large") or m.get("coverImage", {}).get("medium")
    db = get_db()
    db.execute(
        "UPDATE manga SET title=?, slug=?, author=?, artist=?, genre=?, summary=?, status=?, total_chapters=?, year=?, cover=?, source=?, source_id=?, updated_at=? WHERE id=?",
        (title, slugify(title),
         authors[0] if authors else manga["author"],
         authors[1] if len(authors) > 1 else manga["artist"],
         ", ".join(m.get("genres", [])),
         (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
         m.get("status", "").upper() if m.get("status") else manga["status"],
         m.get("chapters") or manga["total_chapters"],
         m.get("startDate", {}).get("year") or manga["year"],
         ext_cover, "anilist", external_id, datetime.now().isoformat(), manga_id)
    )
    db.commit()
    manga_path = Path(manga["path"])
    if manga_path.is_dir():
        existing = read_nfo(manga_path)
        existing.update({
            "Title": title, "Author": authors[0] if authors else manga["author"],
            "Artist": authors[1] if len(authors) > 1 else manga["artist"],
            "Genre": ", ".join(m.get("genres", [])),
            "Summary": (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
            "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
            "TotalChapters": m.get("chapters") or manga["total_chapters"],
            "Year": m.get("startDate", {}).get("year") or manga["year"],
            "Cover": ext_cover, "Source": "anilist", "SourceId": external_id
        })
        write_nfo(manga_path, existing)
        _metadata_cache.invalidate(manga_id)
    db.close()
    return {"ok": True, "title": title, "cover": ext_cover, "source": "anilist"}


async def _apply_mal_rematch(manga_id, manga, external_id, cover_url):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.jikan.moe/v4/manga/{external_id}/full") as resp:
            result = await resp.json()
    m = result.get("data", {})
    if not m:
        raise HTTPException(404, "Match not found on MyAnimeList")
    title = m.get("title") or m.get("title_english") or manga["title"]
    authors = [a["name"] for a in m.get("authors", [])]
    genres = [g["name"] for g in m.get("genres", [])]
    ext_cover = cover_url or m.get("images", {}).get("jpg", {}).get("large_image_url") or m.get("images", {}).get("jpg", {}).get("image_url")
    db = get_db()
    db.execute(
        "UPDATE manga SET title=?, slug=?, author=?, artist=?, genre=?, summary=?, status=?, total_chapters=?, year=?, publisher=?, cover=?, source=?, source_id=?, updated_at=? WHERE id=?",
        (title, slugify(title),
         authors[0] if authors else manga["author"],
         authors[1] if len(authors) > 1 else manga["artist"],
         ", ".join(genres),
         (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
         m.get("status", "").upper() if m.get("status") else manga["status"],
         m.get("chapters") or manga["total_chapters"],
         m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
         (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
         ext_cover, "myanimelist", external_id, datetime.now().isoformat(), manga_id)
    )
    db.commit()
    manga_path = Path(manga["path"])
    if manga_path.is_dir():
        existing = read_nfo(manga_path)
        existing.update({
            "Title": title, "Author": authors[0] if authors else manga["author"],
            "Artist": authors[1] if len(authors) > 1 else manga["artist"],
            "Genre": ", ".join(genres),
            "Summary": (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
            "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
            "TotalChapters": m.get("chapters") or manga["total_chapters"],
            "Year": m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
            "Publisher": (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
            "Cover": ext_cover, "Source": "myanimelist", "SourceId": external_id
        })
        write_nfo(manga_path, existing)
        _metadata_cache.invalidate(manga_id)
    db.close()
    return {"ok": True, "title": title, "cover": ext_cover, "source": "myanimelist"}


async def _apply_simple_rematch(manga_id, manga, source, external_id, title_override, cover_url):
    db = get_db()
    now = datetime.now().isoformat()
    new_title = title_override or manga["title"]
    new_cover = cover_url or manga["cover"]
    db.execute("UPDATE manga SET title=?, slug=?, cover=?, source=?, source_id=?, updated_at=? WHERE id=?",
               (new_title, slugify(new_title), new_cover, source, external_id, now, manga_id))
    db.commit()
    manga_path = Path(manga["path"])
    if manga_path.is_dir():
        nfo_updates = {"Source": source, "SourceId": external_id}
        if title_override: nfo_updates["Title"] = title_override
        if cover_url: nfo_updates["Cover"] = cover_url
        existing = read_nfo(manga_path)
        existing.update(nfo_updates)
        write_nfo(manga_path, existing)
    _metadata_cache.invalidate(manga_id)
    db.close()
    return {"ok": True, "title": new_title, "cover": new_cover, "source": source}


@router.get("/api/manga/{manga_id}/nfo")
async def get_nfo(manga_id: str):
    cached = _metadata_cache.get(manga_id)
    if cached:
        return cached
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")
    manga_path = Path(manga["path"])
    nfo = {}
    if manga_path.is_dir():
        nfo = read_nfo(manga_path)
    if not nfo:
        nfo = {
            "title": manga["title"], "author": manga["author"], "artist": manga["artist"],
            "genre": manga["genre"], "summary": manga["summary"], "publisher": manga["publisher"],
            "year": manga["year"], "status": manga["status"], "total_chapters": manga["total_chapters"],
            "cover": manga["cover"], "source": manga["source"], "source_id": manga["source_id"]
        }
    _metadata_cache.put(manga_id, nfo)
    return nfo

@router.put("/api/manga/{manga_id}/nfo")
async def update_nfo(manga_id: str, data: NfoUpdate):
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        db.close()
        raise HTTPException(404, "Manga not found")
    updates = {}
    for field in ["title","author","artist","genre","summary","publisher","year","status","total_chapters","cover"]:
        val = getattr(data, field)
        if val is not None:
            updates[field] = val
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [datetime.now().isoformat(), manga_id]
        db.execute(f"UPDATE manga SET {set_clause}, updated_at=? WHERE id=?", vals)
        db.commit()
    db.close()
    manga_path = Path(manga["path"])
    if manga_path.is_dir():
        nfo = {}
        for k, v in updates.items():
            tag_map = {
                "title": "Title", "author": "Author", "artist": "Artist", "genre": "Genre",
                "summary": "Summary", "publisher": "Publisher", "year": "Year", "status": "Status",
                "total_chapters": "TotalChapters", "cover": "Cover"
            }
            nfo[tag_map.get(k, k)] = v
        existing = read_nfo(manga_path)
        existing.update(nfo)
        write_nfo(manga_path, existing)
    _metadata_cache.invalidate(manga_id)
    return {"ok": True}

@router.get("/api/manga/{manga_id}/find-metadata")
async def find_external_metadata(manga_id: str, source: str = "anilist"):
    db = get_db()
    manga = db.execute("SELECT title FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")
    if source == "anilist":
        return await search_anilist(manga["title"], limit=5)
    if source == "myanimelist":
        return await search_myanimelist(manga["title"], limit=5)
    return []

# ── Downloads ───────────────────────────────────────────────────────────

@router.post("/api/download")
async def download_chapter(data: DownloadRequest):
    job_id = create_job(data.source, data.manga_title, data.chapter_num, data.chapter_id)
    return {"job_id": job_id}

@router.get("/api/download/{job_id}")
async def download_progress(job_id: str):
    return get_job(job_id)

@router.post("/api/download-all")
async def download_all_chapters(data: DownloadRequest):
    if data.source.startswith("mangal_"):
        return {"job_ids": [], "total": 0, "error": "Download-all not supported for mangal sources"}
    async with aiohttp.ClientSession() as session:
        url = f"https://api.mangadex.org/manga/{data.chapter_id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=500"
        async with session.get(url) as resp:
            manga_data = await resp.json()
    chapters = manga_data.get("data", [])
    if not chapters:
        raise HTTPException(404, "No chapters found.")
    job_ids = []
    for ch in chapters:
        ch_id = ch["id"]
        ch_num = ch["attributes"].get("chapter")
        if not ch_num:
            continue
        job_id = create_job("mangadex", data.manga_title, str(ch_num), ch_id)
        job_ids.append(job_id)
    return {"job_ids": job_ids, "total": len(job_ids)}

# ── Jobs ──────────────────────────────────────────────────────────────

@router.get("/api/jobs")
async def get_jobs(limit: int = 50):
    return list_jobs(limit)

@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job_route(job_id: str):
    cancel_job(job_id)
    return {"ok": True}

# ── Cache ───────────────────────────────────────────────────────────────

@router.get("/cache/{stem}/{filename}")
async def serve_cache(stem: str, filename: str):
    p = CACHE_DIR / stem / filename
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))
