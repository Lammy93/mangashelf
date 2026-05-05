from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Response, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional, List
import os, zipfile, rarfile, fitz, asyncio, aiohttp, uuid, threading, time, shutil
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3
from passlib.context import CryptContext
from jose import JWTError, jwt

app = FastAPI(title="MangaShelf")

MANGA_DIR = Path("/manga")
DB_PATH = Path("/data/manga.db")
CACHE_DIR = Path("/data/cache")
COVERS_DIR = Path("/data/covers")
for d in [DB_PATH.parent, CACHE_DIR, COVERS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="/app/frontend/templates")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")
app.mount("/manga-files", StaticFiles(directory=str(MANGA_DIR)), name="manga-files")

# ── Auth ──────────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get("SECRET_KEY", "mangashelf-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)

def hash_password(pw): return pwd_ctx.hash(pw)
def verify_password(plain, hashed): return pwd_ctx.verify(plain, hashed)

def create_token(data: dict):
    exp = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode({**data, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            return None
    except JWTError:
        return None
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    db.close()
    return dict(user) if user else None

def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user

def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            avatar TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS manga (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            cover TEXT,
            cover_source TEXT,
            description TEXT,
            author TEXT,
            genres TEXT,
            total_volumes INTEGER DEFAULT 0,
            total_chapters INTEGER DEFAULT 0,
            status TEXT DEFAULT 'local',
            source TEXT,
            source_id TEXT,
            added_at TEXT,
            updated_at TEXT,
            release_date TEXT
        );
        CREATE TABLE IF NOT EXISTS volumes (
            id TEXT PRIMARY KEY,
            manga_id TEXT,
            volume_number REAL,
            title TEXT,
            cover TEXT,
            path TEXT,
            FOREIGN KEY (manga_id) REFERENCES manga(id)
        );
        CREATE TABLE IF NOT EXISTS chapters (
            id TEXT PRIMARY KEY,
            manga_id TEXT,
            volume_id TEXT,
            chapter_number REAL,
            title TEXT,
            path TEXT,
            pages INTEGER DEFAULT 0,
            source_url TEXT,
            downloaded INTEGER DEFAULT 0,
            release_date TEXT,
            FOREIGN KEY (manga_id) REFERENCES manga(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id)
        );
        CREATE TABLE IF NOT EXISTS user_progress (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            manga_id TEXT,
            chapter_id TEXT,
            page INTEGER DEFAULT 0,
            reading_mode TEXT DEFAULT 'single',
            is_read INTEGER DEFAULT 0,
            last_read TEXT,
            UNIQUE(user_id, chapter_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (manga_id) REFERENCES manga(id),
            FOREIGN KEY (chapter_id) REFERENCES chapters(id)
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
        CREATE INDEX IF NOT EXISTS idx_chapters_manga ON chapters(manga_id);
        CREATE INDEX IF NOT EXISTS idx_chapters_volume ON chapters(volume_id);
        CREATE INDEX IF NOT EXISTS idx_progress_user ON user_progress(user_id);
        CREATE INDEX IF NOT EXISTS idx_manga_title ON manga(title);
        CREATE INDEX IF NOT EXISTS idx_volumes_manga ON volumes(manga_id);
        INSERT OR IGNORE INTO sources VALUES ('mangadex','MangaDex','https://api.mangadex.org','mangadex',1,datetime('now'));
        INSERT OR IGNORE INTO sources VALUES ('mangasee','MangaSee','https://mangasee123.com','mangasee',1,datetime('now'));
        """)
        # Create default admin if no users exist
        admin = db.execute("SELECT id FROM users WHERE role='admin'").fetchone()
        if not admin:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), "admin", hash_password("admin"), "admin", None, datetime.now().isoformat()))
            db.commit()

init_db()

# ── Models ────────────────────────────────────────────────────────────────────

class ReadingProgress(BaseModel):
    chapter_id: str
    page: int
    reading_mode: Optional[str] = "single"

class ReadingModeUpdate(BaseModel):
    mode: str

class SourceAdd(BaseModel):
    name: str
    base_url: str

class UserCreate(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"

class DownloadRequest(BaseModel):
    chapter_id: str
    manga_title: str
    chapter_num: str
    source: str

# ── Cover Scraping ────────────────────────────────────────────────────────────

async def fetch_cover_mangadex(title: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        url = f"https://api.mangadex.org/manga?title={title}&limit=5&includes[]=cover_art"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
        for m in data.get("data", []):
            attr = m["attributes"]
            t = attr["title"].get("en") or next(iter(attr["title"].values()), "")
            if t.lower() == title.lower() or title.lower() in t.lower():
                cover_rel = next((r for r in m["relationships"] if r["type"] == "cover_art"), None)
                if cover_rel and cover_rel.get("attributes"):
                    fname = cover_rel["attributes"]["fileName"]
                    img_url = f"https://uploads.mangadex.org/covers/{m['id']}/{fname}.512.jpg"
                    async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            cover_path = COVERS_DIR / f"{uuid.uuid4()}.jpg"
                            cover_path.write_bytes(await r.read())
                            return f"/covers/{cover_path.name}"
    except:
        pass
    return None

async def fetch_cover_myanimelist(title: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        url = f"https://api.jikan.moe/v4/manga?q={title}&limit=5"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
        for m in data.get("data", []):
            t = m.get("title", "")
            if t.lower() == title.lower() or title.lower() in t.lower():
                img_url = m.get("images", {}).get("jpg", {}).get("large_image_url")
                if img_url:
                    async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            cover_path = COVERS_DIR / f"{uuid.uuid4()}.jpg"
                            cover_path.write_bytes(await r.read())
                            return f"/covers/{cover_path.name}"
    except:
        pass
    return None

async def scrape_cover(manga_id: str, title: str):
    async with aiohttp.ClientSession() as session:
        cover = await fetch_cover_mangadex(title, session)
        if not cover:
            cover = await fetch_cover_myanimelist(title, session)
        if cover:
            db = get_db()
            db.execute("UPDATE manga SET cover=?, cover_source='scraped' WHERE id=?", (cover, manga_id))
            db.commit()
            db.close()

async def scrape_all_covers():
    db = get_db()
    manga_list = db.execute("SELECT id, title FROM manga WHERE cover IS NULL OR cover_source='local'").fetchall()
    db.close()
    for m in manga_list:
        await scrape_cover(m["id"], m["title"])
        await asyncio.sleep(0.5)

# ── File Helpers ──────────────────────────────────────────────────────────────

SUPPORTED_FORMATS = {'.cbz', '.cbr', '.pdf', '.zip', '.rar', '.epub'}
_page_cache: dict = {}
_cache_lock = threading.Lock()

def extract_pages(file_path: Path) -> List[str]:
    cache_key = str(file_path)
    with _cache_lock:
        if cache_key in _page_cache:
            return _page_cache[cache_key]
    suffix = file_path.suffix.lower()
    out_dir = CACHE_DIR / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted([f for f in out_dir.iterdir() if f.suffix in {'.jpg','.png','.webp'}])
    if existing:
        result = [f"/cache/{file_path.stem}/{p.name}" for p in existing]
        with _cache_lock:
            _page_cache[cache_key] = result
        return result
    if suffix in ('.cbz', '.zip'):
        with zipfile.ZipFile(file_path) as z:
            imgs = sorted([n for n in z.namelist() if Path(n).suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif'}])
            for img in imgs:
                (out_dir / Path(img).name).write_bytes(z.read(img))
    elif suffix in ('.cbr', '.rar'):
        with rarfile.RarFile(file_path) as r:
            imgs = sorted([n for n in r.namelist() if Path(n).suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif'}])
            for img in imgs:
                r.extract(img, out_dir)
    elif suffix == '.pdf':
        doc = fitz.open(str(file_path))
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=120)
            pix.save(str(out_dir / f"page_{i:04d}.jpg"))
    pages = sorted([f for f in out_dir.iterdir() if f.suffix in {'.jpg','.jpeg','.png','.webp','.gif'}])
    result = [f"/cache/{file_path.stem}/{p.name}" for p in pages]
    with _cache_lock:
        _page_cache[cache_key] = result
    return result

_scan_lock = threading.Lock()
_last_scan = 0

def scan_manga_dir(force=False):
    global _last_scan
    now = time.time()
    if not force and now - _last_scan < 30:
        return
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        _last_scan = now
        db = get_db()
        for item in MANGA_DIR.iterdir():
            if not item.is_dir():
                continue
            manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
            existing = db.execute("SELECT id FROM manga WHERE path=?", (str(item),)).fetchone()
            if existing:
                continue

            # Detect volumes (subdirs) vs flat chapters
            subdirs = [d for d in item.iterdir() if d.is_dir()]
            flat_files = sorted([f for f in item.iterdir() if f.suffix.lower() in SUPPORTED_FORMATS])

            db.execute(
                "INSERT OR IGNORE INTO manga VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (manga_id, item.name, str(item), None, None, None, None, None,
                 len(subdirs) if subdirs else 0,
                 len(flat_files),
                 'local', None, None,
                 datetime.now().isoformat(), datetime.now().isoformat(), None)
            )

            if subdirs:
                # Treat subdirs as volumes
                for vi, vdir in enumerate(sorted(subdirs)):
                    vol_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(vdir)))
                    vol_files = sorted([f for f in vdir.iterdir() if f.suffix.lower() in SUPPORTED_FORMATS])
                    db.execute("INSERT OR IGNORE INTO volumes VALUES (?,?,?,?,?,?)",
                        (vol_id, manga_id, float(vi+1), vdir.name, None, str(vdir)))
                    for ci, ch in enumerate(vol_files):
                        ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(ch)))
                        db.execute("INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ch_id, manga_id, vol_id, float(ci+1), ch.stem, str(ch), 0, None, 1, None))
                db.execute("UPDATE manga SET total_volumes=?, total_chapters=? WHERE id=?",
                    (len(subdirs), sum(len(sorted([f for f in vd.iterdir() if f.suffix.lower() in SUPPORTED_FORMATS])) for vd in subdirs), manga_id))
            else:
                # Flat chapters
                for ci, ch in enumerate(flat_files):
                    ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(ch)))
                    db.execute("INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ch_id, manga_id, None, float(ci+1), ch.stem, str(ch), 0, None, 1, None))
                db.execute("UPDATE manga SET total_chapters=? WHERE id=?", (len(flat_files), manga_id))

        db.commit()
        db.close()
    finally:
        _scan_lock.release()

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
async def do_login(request: Request, response: Response):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    if not user or not verify_password(password, user["password"]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})
    token = create_token({"sub": user["id"]})
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, max_age=86400*30, samesite="lax")
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp

@app.post("/api/auth/token")
async def api_token(form_data: OAuth2PasswordRequestForm = Depends()):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (form_data.username,)).fetchone()
    db.close()
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(401, "Incorrect credentials")
    return {"access_token": create_token({"sub": user["id"]}), "token_type": "bearer"}

# ── Page Routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/manga/{manga_id}")
async def manga_detail(request: Request, manga_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("manga_detail.html", {"request": request, "user": user, "manga_id": manga_id})

@app.get("/read/{manga_id}/{chapter_id}")
async def reader(request: Request, manga_id: str, chapter_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("reader.html", {"request": request, "manga_id": manga_id, "chapter_id": chapter_id, "user": user})

@app.get("/sources")
async def sources_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("sources.html", {"request": request, "user": user})

@app.get("/search")
async def search_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("search.html", {"request": request, "user": user})

@app.get("/admin")
async def admin_page(request: Request):
    user = require_admin(request)
    return templates.TemplateResponse("admin.html", {"request": request, "user": user})

# ── API: Library ──────────────────────────────────────────────────────────────

@app.get("/api/library")
async def get_library(request: Request, q: str = ""):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    scan_manga_dir()
    db = get_db()
    if q:
        rows = db.execute("SELECT * FROM manga WHERE title LIKE ? ORDER BY title", (f"%{q}%",)).fetchall()
    else:
        rows = db.execute("SELECT * FROM manga ORDER BY title").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/recently-added")
async def recently_added(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    rows = db.execute("SELECT * FROM manga ORDER BY added_at DESC LIMIT 12").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/continue-reading")
async def continue_reading(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    rows = db.execute("""
        SELECT m.*, up.chapter_id, up.page, up.last_read, up.reading_mode,
               c.chapter_number, c.title as chapter_title
        FROM user_progress up
        JOIN manga m ON m.id = up.manga_id
        JOIN chapters c ON c.id = up.chapter_id
        WHERE up.user_id=? AND up.is_read=0 AND up.page > 0
        ORDER BY up.last_read DESC
        LIMIT 12
    """, (user["id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/manga/{manga_id}")
async def get_manga(manga_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        raise HTTPException(404)
    volumes = db.execute("SELECT * FROM volumes WHERE manga_id=? ORDER BY volume_number", (manga_id,)).fetchall()
    chapters = db.execute("SELECT * FROM chapters WHERE manga_id=? ORDER BY chapter_number", (manga_id,)).fetchall()

    # Get user progress for each chapter
    progress = {}
    for ch in chapters:
        p = db.execute("SELECT * FROM user_progress WHERE user_id=? AND chapter_id=?",
                       (user["id"], ch["id"])).fetchone()
        if p:
            progress[ch["id"]] = dict(p)

    db.close()
    return {
        "manga": dict(manga),
        "volumes": [dict(v) for v in volumes],
        "chapters": [dict(c) for c in chapters],
        "progress": progress
    }

@app.get("/api/chapter/{chapter_id}/pages")
async def get_pages(chapter_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404)
    loop = asyncio.get_event_loop()
    pages = await loop.run_in_executor(None, extract_pages, Path(ch["path"]))
    if ch["pages"] != len(pages):
        db.execute("UPDATE chapters SET pages=? WHERE id=?", (len(pages), chapter_id))
        db.commit()

    # Get user's saved progress
    prog = db.execute("SELECT * FROM user_progress WHERE user_id=? AND chapter_id=?",
                      (user["id"], chapter_id)).fetchone()
    db.close()

    # Get adjacent chapters
    all_ch = get_db().execute("SELECT id, chapter_number FROM chapters WHERE manga_id=? ORDER BY chapter_number",
                               (ch["manga_id"],)).fetchall()
    ch_ids = [c["id"] for c in all_ch]
    idx = ch_ids.index(chapter_id) if chapter_id in ch_ids else 0
    prev_ch = ch_ids[idx-1] if idx > 0 else None
    next_ch = ch_ids[idx+1] if idx < len(ch_ids)-1 else None

    return {
        "pages": pages,
        "total": len(pages),
        "saved_page": prog["page"] if prog else 0,
        "reading_mode": prog["reading_mode"] if prog else "single",
        "prev_chapter": prev_ch,
        "next_chapter": next_ch
    }

@app.post("/api/progress")
async def save_progress(data: ReadingProgress, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    ch = db.execute("SELECT * FROM chapters WHERE id=?", (data.chapter_id,)).fetchone()
    if not ch:
        raise HTTPException(404)
    prog_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user['id']}-{data.chapter_id}"))
    is_read = 1 if (ch["pages"] > 0 and data.page >= ch["pages"] - 1) else 0
    db.execute("""
        INSERT INTO user_progress (id, user_id, manga_id, chapter_id, page, reading_mode, is_read, last_read)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id, chapter_id) DO UPDATE SET
            page=excluded.page, reading_mode=excluded.reading_mode,
            is_read=excluded.is_read, last_read=excluded.last_read
    """, (prog_id, user["id"], ch["manga_id"], data.chapter_id, data.page,
          data.reading_mode, is_read, datetime.now().isoformat()))
    db.commit()
    db.close()
    return {"ok": True}

# ── API: Covers ───────────────────────────────────────────────────────────────

@app.post("/api/manga/{manga_id}/scrape-cover")
async def scrape_manga_cover(manga_id: str, request: Request, background_tasks: BackgroundTasks):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404)
    background_tasks.add_task(scrape_cover, manga_id, manga["title"])
    return {"ok": True, "message": "Cover scraping started"}

@app.post("/api/scrape-all-covers")
async def scrape_covers_all(request: Request, background_tasks: BackgroundTasks):
    user = require_admin(request)
    background_tasks.add_task(scrape_all_covers)
    return {"ok": True, "message": "Scraping all covers in background"}

# ── API: Sources ──────────────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources():
    db = get_db()
    rows = db.execute("SELECT * FROM sources ORDER BY name").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/sources")
async def add_source(data: SourceAdd, request: Request):
    require_admin(request)
    db = get_db()
    src_id = str(uuid.uuid4())
    db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",
               (src_id, data.name, data.base_url, 'custom', 1, datetime.now().isoformat()))
    db.commit()
    db.close()
    return {"id": src_id}

@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    db.commit()
    db.close()
    return {"ok": True}

@app.patch("/api/sources/{source_id}/toggle")
async def toggle_source(source_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    db.commit()
    db.close()
    return {"ok": True}

# ── API: Users (admin) ────────────────────────────────────────────────────────

@app.get("/api/users")
async def get_users(request: Request):
    require_admin(request)
    db = get_db()
    rows = db.execute("SELECT id, username, role, created_at FROM users ORDER BY username").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/users")
async def create_user(data: UserCreate, request: Request):
    require_admin(request)
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username=?", (data.username,)).fetchone()
    if existing:
        raise HTTPException(400, "Username already exists")
    uid = str(uuid.uuid4())
    db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
               (uid, data.username, hash_password(data.password), data.role, None, datetime.now().isoformat()))
    db.commit()
    db.close()
    return {"id": uid, "username": data.username, "role": data.role}

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    db.close()
    return {"ok": True}

# ── API: Search ───────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_manga(q: str, source: str = "mangadex"):
    if source == "mangadex":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga?title={q}&limit=20&includes[]=cover_art&contentRating[]=safe&contentRating[]=suggestive"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                "id": m["id"], "title": title, "cover": cover,
                "status": attr.get("status"), "source": "mangadex",
                "description": next(iter(attr.get("description", {}).values()), "")[:200]
            })
        return results
    return []

@app.get("/api/manga-source/{source}/{manga_id}/chapters")
async def get_source_chapters(source: str, manga_id: str):
    if source == "mangadex":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga/{manga_id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        return [{"id": ch["id"], "chapter": ch["attributes"].get("chapter"),
                 "title": ch["attributes"].get("title") or f"Chapter {ch['attributes'].get('chapter','')}",
                 "pages": ch["attributes"].get("pages", 0), "source": "mangadex"}
                for ch in data.get("data", [])]
    return []

# ── API: Downloads ────────────────────────────────────────────────────────────

download_status = {}

async def download_mangadex_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        download_status[job_id] = {"status": "downloading", "progress": 0}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.mangadex.org/at-home/server/{chapter_id}",
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
            base = data["baseUrl"]
            hash_ = data["chapter"]["hash"]
            pages = data["chapter"]["data"]
            out_dir = MANGA_DIR / manga_title
            out_dir.mkdir(parents=True, exist_ok=True)
            cbz_path = out_dir / f"Chapter_{float(chapter_num):06.1f}.cbz"
            imgs = []
            for i, page in enumerate(pages):
                async with session.get(f"{base}/data/{hash_}/{page}") as r:
                    imgs.append((page, await r.read()))
                download_status[job_id]["progress"] = int((i+1)/len(pages)*100)
            with zipfile.ZipFile(cbz_path, 'w', compression=zipfile.ZIP_STORED) as z:
                for name, d in imgs:
                    z.writestr(name, d)
        download_status[job_id] = {"status": "complete", "progress": 100}
        scan_manga_dir(force=True)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

@app.post("/api/download")
async def download_chapter(data: DownloadRequest, background_tasks: BackgroundTasks, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    job_id = str(uuid.uuid4())
    if data.source == "mangadex":
        background_tasks.add_task(download_mangadex_chapter, data.chapter_id, data.manga_title, data.chapter_num, job_id)
    return {"job_id": job_id}

@app.get("/api/download/{job_id}")
async def download_progress(job_id: str):
    return download_status.get(job_id, {"status": "unknown"})

# ── Static cache & covers ─────────────────────────────────────────────────────

@app.get("/cache/{stem}/{filename}")
async def serve_cache(stem: str, filename: str):
    p = CACHE_DIR / stem / filename
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=31536000, immutable"})

@app.get("/covers/{filename}")
async def serve_cover(filename: str):
    p = COVERS_DIR / filename
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=86400"})
