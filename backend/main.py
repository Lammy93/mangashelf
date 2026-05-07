from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
import os, json, zipfile, rarfile, fitz, shutil, asyncio, aiohttp, uuid, base64, hmac, threading, time
from pathlib import Path
from datetime import datetime
import sqlite3
import hashlib
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(pw):
    return pwd_context.hash(pw)

def verify_password(pw, hashed):
    return pwd_context.verify(pw, hashed)

app = FastAPI(title="MangaShelf")

MANGA_DIR = Path("/manga")
DB_PATH = Path("/data/manga.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="/app/frontend/templates")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")
app.mount("/manga-files", StaticFiles(directory=str(MANGA_DIR)), name="manga-files")

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS manga_directories (
            id TEXT PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            enabled INTEGER DEFAULT 1,
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        INSERT OR IGNORE INTO scan_settings VALUES ('scan_interval', '300');
        INSERT OR IGNORE INTO scan_settings VALUES ('auto_scan_enabled', '1');
        INSERT OR IGNORE INTO scan_settings VALUES ('watch_enabled', '1');
        CREATE TABLE IF NOT EXISTS manga (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            cover TEXT,
            total_chapters INTEGER DEFAULT 0,
            last_read_chapter INTEGER DEFAULT 0,
            last_read_page INTEGER DEFAULT 0,
            reading_mode TEXT DEFAULT 'single',
            source TEXT,
            source_id TEXT,
            added_at TEXT,
            updated_at TEXT,
            status TEXT DEFAULT 'local'
        );
        CREATE TABLE IF NOT EXISTS chapters (
            id TEXT PRIMARY KEY,
            manga_id TEXT,
            chapter_number REAL,
            title TEXT,
            path TEXT,
            pages INTEGER DEFAULT 0,
            read_page INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0,
            source_url TEXT,
            downloaded INTEGER DEFAULT 0,
            FOREIGN KEY (manga_id) REFERENCES manga(id)
        );
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            type TEXT DEFAULT 'custom',
            enabled INTEGER DEFAULT 1,
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS download_queue (
            id TEXT PRIMARY KEY,
            manga_id TEXT,
            chapter_id TEXT,
            status TEXT DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            added_at TEXT,
            error TEXT
        );
        INSERT OR IGNORE INTO sources VALUES ('mangadex','MangaDex','https://api.mangadex.org','mangadex',1,datetime('now'));
        INSERT OR IGNORE INTO sources VALUES ('mangasee','MangaSee','https://mangasee123.com','mangasee',1,datetime('now'));
        """)

init_db()

def get_scan_setting(key: str) -> str:
    db = get_db()
    row = db.execute("SELECT value FROM scan_settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else ""

def set_scan_setting(key: str, value: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO scan_settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    db.close()

def get_manga_directories():
    db = get_db()
    rows = db.execute("SELECT * FROM manga_directories WHERE enabled=1 ORDER BY added_at").fetchall()
    db.close()
    return [dict(r) for r in rows]

def get_all_manga_directories():
    db = get_db()
    rows = db.execute("SELECT * FROM manga_directories ORDER BY added_at").fetchall()
    db.close()
    return [dict(r) for r in rows]

def init_default_directory():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM manga_directories").fetchone()
    if count["cnt"] == 0:
        db.execute(
            "INSERT INTO manga_directories (id, path, enabled, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "/manga", 1, datetime.now().isoformat())
        )
        db.commit()
    db.close()

init_default_directory()

SESSION_SECRET = os.environ.get("SECRET_KEY", "change-this-to-a-long-random-string")

def is_first_launch():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    db.close()
    return count["cnt"] == 0


SESSION_SECRET = os.environ.get("SECRET_KEY", "change-this-to-a-long-random-string")

def create_session_token(user_id: str, username: str, role: str) -> str:
    payload = base64.b64encode(json.dumps({"uid": user_id, "user": username, "role": role}).encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_session_token(token: str) -> Optional[dict]:
    try:
        payload, sig = token.split(".")
        if hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest() != sig:
            return None
        return json.loads(base64.b64decode(payload))
    except Exception:
        return None

async def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("session")
    if not token:
        return None
    return verify_session_token(token)

def require_auth(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = verify_session_token(token)
    if not user:
        raise HTTPException(401, "Invalid session")
    return user

def require_admin(request: Request):
    user = require_auth(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user

# ── Folder Watcher ────────────────────────────────────────────────────────────

_scan_lock = threading.Lock()
_scan_pending = False
_last_scan = 0.0
_watcher = None

def get_scan_cooldown():
    interval = int(get_scan_setting("scan_interval") or 300)
    return max(5, interval / 2)

def debounced_scan():
    global _scan_pending, _last_scan
    now = time.time()
    cooldown = get_scan_cooldown()
    if now - _last_scan < cooldown:
        return
    with _scan_lock:
        if not _scan_pending:
            _scan_pending = True
            def do_scan():
                global _scan_pending, _last_scan
                try:
                    scan_manga_dir()
                finally:
                    _scan_pending = False
                    _last_scan = time.time()
            threading.Thread(target=do_scan, daemon=True).start()

class MangaDirHandler(FileSystemEventHandler):
    SUPPORTED_EXTS = {'.cbz', '.cbr', '.pdf', '.zip', '.rar', '.epub'}

    def _should_trigger(self, event: FileSystemEvent) -> bool:
        if event.is_directory:
            return True
        return Path(event.src_path).suffix.lower() in self.SUPPORTED_EXTS

    def on_created(self, event: FileSystemEvent):
        if self._should_trigger(event):
            debounced_scan()

    def on_deleted(self, event: FileSystemEvent):
        if self._should_trigger(event):
            debounced_scan()

    def on_moved(self, event: FileSystemEvent):
        debounced_scan()

    def on_closed(self, event: FileSystemEvent):
        if self._should_trigger(event):
            debounced_scan()

def start_folder_watcher():
    global _watcher
    if _watcher:
        _watcher.stop()
    if get_scan_setting("watch_enabled") != "1":
        return
    _watcher = Observer()
    directories = get_manga_directories()
    for dir_conf in directories:
        d = Path(dir_conf["path"])
        if d.exists():
            _watcher.schedule(MangaDirHandler(), str(d), recursive=True)
    _watcher.daemon = True
    _watcher.start()

def start_interval_scanner():
    if get_scan_setting("auto_scan_enabled") != "1":
        return
    def interval_loop():
        while True:
            interval = int(get_scan_setting("scan_interval") or 300)
            time.sleep(interval)
            if get_scan_setting("auto_scan_enabled") != "1":
                break
            scan_manga_dir()
    threading.Thread(target=interval_loop, daemon=True).start()

start_folder_watcher()
start_interval_scanner()

# ── Models ────────────────────────────────────────────────────────────────────

class SourceAdd(BaseModel):
    name: str
    base_url: str

class ReadingProgress(BaseModel):
    chapter_id: str
    page: int

class ReadingModeUpdate(BaseModel):
    mode: str  # single | double | strip

# ── Helpers ───────────────────────────────────────────────────────────────────

SUPPORTED_FORMATS = {'.cbz', '.cbr', '.pdf', '.zip', '.rar', '.epub'}

def extract_pages(file_path: Path) -> List[str]:
    """Return list of page image paths or base64 for a chapter file."""
    suffix = file_path.suffix.lower()
    out_dir = Path("/data/cache") / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = sorted([f for f in out_dir.iterdir() if f.suffix in {'.jpg','.png','.webp'}])
    if pages:
        return [f"/cache/{file_path.stem}/{p.name}" for p in pages]

    if suffix in ('.cbz', '.zip'):
        with zipfile.ZipFile(file_path) as z:
            imgs = sorted([n for n in z.namelist() if Path(n).suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif'}])
            for img in imgs:
                z.extract(img, out_dir)
    elif suffix in ('.cbr', '.rar'):
        with rarfile.RarFile(file_path) as r:
            imgs = sorted([n for n in r.namelist() if Path(n).suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif'}])
            for img in imgs:
                r.extract(img, out_dir)
    elif suffix == '.pdf':
        doc = fitz.open(str(file_path))
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=150)
            pix.save(str(out_dir / f"page_{i:04d}.png"))

    pages = sorted([f for f in out_dir.iterdir() if f.suffix in {'.jpg','.jpeg','.png','.webp','.gif'}])
    return [f"/cache/{file_path.stem}/{p.name}" for p in pages]

def scan_manga_dir():
    """Scan all configured manga directories and update database."""
    directories = get_manga_directories()
    db = get_db()
    for dir_conf in directories:
        scan_path = Path(dir_conf["path"])
        if not scan_path.exists():
            continue
        for item in scan_path.iterdir():
            if item.is_dir():
                manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
                existing = db.execute("SELECT id FROM manga WHERE path=?", (str(item),)).fetchone()
                if not existing:
                    cover = None
                    chapters = []
                    for f in sorted(item.iterdir()):
                        if f.suffix.lower() in SUPPORTED_FORMATS:
                            chapters.append(f)
                    if chapters:
                        try:
                            pages = extract_pages(chapters[0])
                            if pages:
                                cover = pages[0]
                        except:
                            pass
                    db.execute(
    """
    INSERT OR IGNORE INTO manga (
        id,
        title,
        path,
        cover,
        total_chapters,
        last_read_chapter,
        last_read_page,
        reading_mode,
        source,
        source_id,
        added_at,
        updated_at,
        status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        manga_id,
        item.name,
        str(item),
        cover,
        len(chapters),
        0,
        0,
        'single',
        None,
        None,
        datetime.now().isoformat(),
        datetime.now().isoformat(),
        'local'
    )
)
                for i, ch in enumerate(chapters):
                    ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(ch)))
                    db.execute(
                        "INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ch_id, manga_id, float(i+1), ch.stem, str(ch), 0, 0, 0, None, 1)
                    )
            elif item.suffix.lower() in SUPPORTED_FORMATS:
                manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
                existing = db.execute("SELECT id FROM manga WHERE path=?", (str(item),)).fetchone()
                if not existing:
                    try:
                        pages = extract_pages(item)
                        cover = pages[0] if pages else None
                    except:
                        cover = None
                    db.execute("""
INSERT OR IGNORE INTO manga (
    id,
    title,
    path,
    cover,
    total_chapters,
    last_read_chapter,
    last_read_page,
    reading_mode,
    source,
    source_id,
    added_at,
    updated_at,
    status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    manga_id,
    item.stem,
    str(item),
    cover,
    1,
    0,
    0,
    'single',
    None,
    None,
    datetime.now().isoformat(),
    datetime.now().isoformat(),
    'local'
))
                ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
                db.execute(
                    "INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ch_id, manga_id, 1.0, item.stem, str(item), 0, 0, 0, None, 1)
                )
    db.commit()
    db.close()

# ── Routes: Pages ─────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    if is_first_launch():
        return RedirectResponse(url="/setup")
    user = await get_current_user(request)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user}
    )

@app.get("/manga/{manga_id}")
async def manga_detail(request: Request, manga_id: str):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("manga_detail.html", {"request": request, "manga_id": manga_id, "user": user})

@app.get("/setup")
async def setup_page(request: Request):
    if not is_first_launch():
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/setup")
async def setup_post(request: Request):
    if not is_first_launch():
        return RedirectResponse(url="/login")
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    confirm = form.get("confirm_password", "")
    if len(username) < 3:
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Username must be at least 3 characters."})
    if len(password) < 6:
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Password must be at least 6 characters."})
    if password != confirm:
        return templates.TemplateResponse("setup.html", {"request": request, "error": "Passwords do not match."})
    db = get_db()
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), username, hash_password(password), "admin", datetime.now().isoformat())
    )
    db.commit()
    user_row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    token = create_session_token(user_row["id"], user_row["username"], user_row["role"])
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.get("/login")
async def login_page(request: Request):
    if is_first_launch():
        return RedirectResponse(url="/setup")
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password."})
    token = create_session_token(user["id"], user["username"], user["role"])
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="session")
    return response

@app.get("/admin")
async def admin_page(request: Request):
    user = require_admin(request)
    return templates.TemplateResponse("admin.html", {"request": request, "user": user})

@app.get("/read/{manga_id}/{chapter_id}")
async def reader(request: Request, manga_id: str, chapter_id: str):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("reader.html", {"request": request, "manga_id": manga_id, "chapter_id": chapter_id, "user": user})

@app.get("/sources")
async def sources_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("sources.html", {"request": request, "user": user})

@app.get("/search")
async def search_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("search.html", {"request": request, "user": user})

# ── Routes: API: Library ──────────────────────────────────────────────────────

@app.get("/api/library")
async def get_library(q: str = ""):
    scan_manga_dir()
    db = get_db()
    if q:
        rows = db.execute("SELECT * FROM manga WHERE title LIKE ? ORDER BY title", (f"%{q}%",)).fetchall()
    else:
        rows = db.execute("SELECT * FROM manga ORDER BY title").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/continue-reading")
async def get_continue_reading():
    db = get_db()
    rows = db.execute("""
        SELECT m.id, m.title, m.cover, c.id as chapter_id, c.chapter_number, c.title as chapter_title,
               c.read_page as page, c.pages
        FROM manga m
        JOIN chapters c ON c.manga_id = m.id
        WHERE c.read_page > 0 AND c.is_read = 0
        ORDER BY m.updated_at DESC
        LIMIT 10
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/recently-added")
async def get_recently_added():
    scan_manga_dir()
    db = get_db()
    rows = db.execute("SELECT * FROM manga ORDER BY added_at DESC LIMIT 8").fetchall()
    db.close()
    return [dict(r) for r in rows]

# ── Routes: API: Scan Settings ────────────────────────────────────────────────

class MangaDirAdd(BaseModel):
    path: str

class ScanSettingsUpdate(BaseModel):
    scan_interval: Optional[int] = None
    auto_scan_enabled: Optional[bool] = None
    watch_enabled: Optional[bool] = None

@app.get("/api/scan-settings")
async def get_scan_settings(request: Request):
    require_admin(request)
    return {
        "scan_interval": int(get_scan_setting("scan_interval") or 300),
        "auto_scan_enabled": get_scan_setting("auto_scan_enabled") == "1",
        "watch_enabled": get_scan_setting("watch_enabled") == "1",
        "directories": get_all_manga_directories()
    }

@app.post("/api/scan-settings")
async def update_scan_settings(data: ScanSettingsUpdate, request: Request):
    require_admin(request)
    if data.scan_interval is not None:
        set_scan_setting("scan_interval", str(max(30, data.scan_interval)))
    if data.auto_scan_enabled is not None:
        set_scan_setting("auto_scan_enabled", "1" if data.auto_scan_enabled else "0")
    if data.watch_enabled is not None:
        set_scan_setting("watch_enabled", "1" if data.watch_enabled else "0")
        start_folder_watcher()
    return get_scan_settings(request)

@app.get("/api/scan-directories")
async def get_scan_directories(request: Request):
    require_admin(request)
    return get_all_manga_directories()

@app.post("/api/scan-directories")
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

@app.delete("/api/scan-directories/{dir_id}")
async def delete_scan_directory(dir_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM manga_directories WHERE id=?", (dir_id,))
    db.commit()
    db.close()
    start_folder_watcher()
    return {"ok": True}

@app.patch("/api/scan-directories/{dir_id}/toggle")
async def toggle_scan_directory(dir_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE manga_directories SET enabled = 1 - enabled WHERE id=?", (dir_id,))
    db.commit()
    db.close()
    start_folder_watcher()
    scan_manga_dir()
    return {"ok": True}

@app.post("/api/scan-now")
async def scan_now(request: Request):
    require_admin(request)
    scan_manga_dir()
    return {"ok": True}

@app.get("/api/manga/{manga_id}")
async def get_manga(manga_id: str):
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        raise HTTPException(404, "Manga not found")
    chapters = db.execute("SELECT * FROM chapters WHERE manga_id=? ORDER BY chapter_number", (manga_id,)).fetchall()
    db.close()
    return {"manga": dict(manga), "chapters": [dict(c) for c in chapters]}

@app.get("/api/chapter/{chapter_id}/pages")
async def get_pages(chapter_id: str):
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404, "Chapter not found")
    pages = extract_pages(Path(ch["path"]))
    db.execute("UPDATE chapters SET pages=? WHERE id=?", (len(pages), chapter_id))
    db.commit()
    db.close()
    return {"pages": pages, "total": len(pages)}

# ── Routes: API: Users ────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

@app.get("/api/users")
async def get_users(request: Request):
    require_admin(request)
    db = get_db()
    rows = db.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/users")
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
    db.commit()
    db.close()
    return {"ok": True}

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    user = require_admin(request)
    if user["uid"] == user_id:
        raise HTTPException(400, "Cannot delete yourself.")
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    db.close()
    return {"ok": True}

@app.post("/api/progress")
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
    db.commit()
    db.close()
    return {"ok": True}

@app.post("/api/manga/{manga_id}/reading-mode")
async def set_reading_mode(manga_id: str, data: ReadingModeUpdate):
    db = get_db()
    db.execute("UPDATE manga SET reading_mode=? WHERE id=?", (data.mode, manga_id))
    db.commit()
    db.close()
    return {"ok": True}

# ── Routes: API: Sources ──────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources():
    db = get_db()
    rows = db.execute("SELECT * FROM sources ORDER BY name").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/sources")
async def add_source(data: SourceAdd):
    db = get_db()
    src_id = str(uuid.uuid4())
    db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",
               (src_id, data.name, data.base_url, 'custom', 1, datetime.now().isoformat()))
    db.commit()
    db.close()
    return {"id": src_id, "name": data.name, "base_url": data.base_url}

@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: str):
    db = get_db()
    db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    db.commit()
    db.close()
    return {"ok": True}

@app.patch("/api/sources/{source_id}/toggle")
async def toggle_source(source_id: str):
    db = get_db()
    db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    db.commit()
    db.close()
    return {"ok": True}

# ── Routes: API: Search (MangaDex) ────────────────────────────────────────────

@app.get("/api/search")
async def search_manga(q: str, source: str = "mangadex"):
    if source == "mangadex":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga?title={q}&limit=20&includes[]=cover_art&contentRating[]=safe&contentRating[]=suggestive"
            async with session.get(url) as resp:
                data = await resp.json()
        results = []
        for m in data.get("data", []):
            attr = m["attributes"]
            title = attr["title"].get("en") or next(iter(attr["title"].values()), "Unknown")
            cover_rel = next((r for r in m["relationships"] if r["type"] == "cover_art"), None)
            cover = None
            if cover_rel and cover_rel.get("attributes"):
                fname = cover_rel["attributes"]["fileName"]
                cover = f"https://uploads.mangadex.org/covers/{m['id']}/{fname}.256.jpg"
            results.append({
                "id": m["id"],
                "title": title,
                "cover": cover,
                "status": attr.get("status"),
                "source": "mangadex",
                "description": next(iter(attr.get("description", {}).values()), "")[:200]
            })
        return results
    return []

@app.get("/api/manga-source/{source}/{manga_id}/chapters")
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
                "id": ch["id"],
                "chapter": attr.get("chapter"),
                "title": attr.get("title") or f"Chapter {attr.get('chapter','')}",
                "pages": attr.get("pages", 0),
                "source": "mangadex"
            })
        return chapters
    return []

# ── Routes: API: Downloads ────────────────────────────────────────────────────

download_status = {}

async def download_mangadex_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        download_status[job_id] = {"status": "downloading", "progress": 0, "error": None}
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/at-home/server/{chapter_id}"
            async with session.get(url) as resp:
                data = await resp.json()
            base = data["baseUrl"]
            hash_ = data["chapter"]["hash"]
            pages = data["chapter"]["data"]
            out_dir = MANGA_DIR / manga_title
            out_dir.mkdir(parents=True, exist_ok=True)
            cbz_path = out_dir / f"Chapter_{float(chapter_num):06.1f}.cbz"
            imgs = []
            for i, page in enumerate(pages):
                img_url = f"{base}/data/{hash_}/{page}"
                async with session.get(img_url) as r:
                    imgs.append((page, await r.read()))
                download_status[job_id]["progress"] = int((i+1)/len(pages)*100)
            with zipfile.ZipFile(cbz_path, 'w') as z:
                for name, data_ in imgs:
                    z.writestr(name, data_)
        download_status[job_id] = {"status": "complete", "progress": 100, "error": None}
        scan_manga_dir()
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

class DownloadRequest(BaseModel):
    chapter_id: str
    manga_title: str
    chapter_num: str
    source: str

@app.post("/api/download")
async def download_chapter(data: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    if data.source == "mangadex":
        background_tasks.add_task(download_mangadex_chapter, data.chapter_id, data.manga_title, data.chapter_num, job_id)
    return {"job_id": job_id}

@app.get("/api/download/{job_id}")
async def download_progress(job_id: str):
    return download_status.get(job_id, {"status": "unknown"})

@app.post("/api/download-all")
async def download_all_chapters(data: DownloadRequest, background_tasks: BackgroundTasks):
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
        job_id = str(uuid.uuid4())
        background_tasks.add_task(download_mangadex_chapter, ch_id, data.manga_title, ch_num, job_id)
        job_ids.append(job_id)
    return {"job_ids": job_ids, "total": len(job_ids)}

# ── Cache static serve ────────────────────────────────────────────────────────

from fastapi.responses import FileResponse as FR

@app.get("/cache/{stem}/{filename}")
async def serve_cache(stem: str, filename: str):
    p = Path("/data/cache") / stem / filename
    if not p.exists():
        raise HTTPException(404)
    return FR(str(p))

