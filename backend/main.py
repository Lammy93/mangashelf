from collections import OrderedDict
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
import xml.etree.ElementTree as ET
import os, json, zipfile, rarfile, fitz, shutil, asyncio, aiohttp, uuid, base64, hmac, threading, time, re
from pathlib import Path
from datetime import datetime
import sqlite3
import hashlib
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[-\s]+', '-', s)
    return s[:80]

def hash_password(pw):
    return pwd_context.hash(pw)

def verify_password(pw, hashed):
    return pwd_context.verify(pw, hashed)

app = FastAPI(title="MangaShelf")

MANGA_DIR = Path("/manga")
DB_PATH = Path("/data/manga.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

class LRUCache:
    def __init__(self, maxsize=128, ttl=60):
        self.maxsize = maxsize
        self.ttl = ttl
        self.data = OrderedDict()
        self.timestamps = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                if time.time() - self.timestamps[key] < self.ttl:
                    self.data.move_to_end(key)
                    return self.data[key]
                del self.data[key]
                del self.timestamps[key]
            return None

    def put(self, key, value):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
            elif len(self.data) >= self.maxsize:
                oldest = next(iter(self.data))
                del self.data[oldest]
                del self.timestamps[oldest]
            self.data[key] = value
            self.timestamps[key] = time.time()

    def invalidate(self, key):
        with self.lock:
            self.data.pop(key, None)
            self.timestamps.pop(key, None)

_metadata_cache = LRUCache(maxsize=256, ttl=120)

def read_nfo(folder_path: Path) -> dict:
    nfo = folder_path / "mangashelf.xml"
    if not nfo.exists():
        return {}
    try:
        tree = ET.parse(str(nfo))
        root = tree.getroot()
        return {
            "title": (root.findtext("Title") or "").strip(),
            "author": (root.findtext("Author") or "").strip(),
            "artist": (root.findtext("Artist") or "").strip(),
            "genre": (root.findtext("Genre") or "").strip(),
            "summary": (root.findtext("Summary") or "").strip(),
            "publisher": (root.findtext("Publisher") or "").strip(),
            "year": int(y) if (y := root.findtext("Year") or "").strip().isdigit() else None,
            "status": (root.findtext("Status") or "").strip(),
            "total_chapters": int(c) if (c := root.findtext("TotalChapters") or "").strip().isdigit() else 0,
            "cover": (root.findtext("Cover") or "").strip(),
            "source_id": (root.findtext("SourceId") or "").strip(),
            "source": (root.findtext("Source") or "").strip(),
        }
    except:
        return {}

def write_nfo(folder_path: Path, meta: dict):
    root = ET.Element("MangaShelf")
    for tag, val in meta.items():
        if val is not None and val != "" and val != 0:
            child = ET.SubElement(root, tag.replace("total_chapters", "TotalChapters").replace("source_id", "SourceId").replace(" ", ""))
            child.text = str(val)
    ET.indent(root)
    nfo = folder_path / "mangashelf.xml"
    ET.ElementTree(root).write(str(nfo), encoding="utf-8", xml_declaration=True)

templates = Jinja2Templates(directory="/app/frontend/templates")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")
app.mount("/manga-files", StaticFiles(directory=str(MANGA_DIR)), name="manga-files")
(Path("/data/avatars").mkdir(parents=True, exist_ok=True))
app.mount("/avatars", StaticFiles(directory="/data/avatars"), name="avatars")

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
            avatar TEXT,
            display_name TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            reading_mode TEXT DEFAULT 'single',
            strip_scroll_sensitivity REAL DEFAULT 1.0,
            auto_hide_toolbar INTEGER DEFAULT 1,
            show_page_numbers INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
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
        INSERT OR IGNORE INTO scan_settings VALUES ('scan_on_folder_change', '1');
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
            status TEXT DEFAULT 'local',
            author TEXT,
            artist TEXT,
            genre TEXT,
            summary TEXT,
            publisher TEXT,
            year INTEGER
        );
        CREATE TABLE IF NOT EXISTS volumes (
            id TEXT PRIMARY KEY,
            manga_id TEXT,
            volume_number INTEGER,
            title TEXT,
            path TEXT,
            total_chapters INTEGER DEFAULT 0,
            cover TEXT,
            summary TEXT,
            FOREIGN KEY (manga_id) REFERENCES manga(id)
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
            volume_id TEXT,
            FOREIGN KEY (manga_id) REFERENCES manga(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id)
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
        INSERT OR IGNORE INTO sources VALUES ('mangakakalot','MangaKakalot','https://mangakakalot.com','mangakakalot',1,datetime('now'));
        INSERT OR IGNORE INTO sources VALUES ('mangafox','MangaFox','https://fanfox.net','mangafox',1,datetime('now'));
        INSERT OR IGNORE INTO sources VALUES ('anilist','AniList','https://graphql.anilist.co','metadata',1,datetime('now'));
        INSERT OR IGNORE INTO sources VALUES ('myanimelist','MyAnimeList','https://api.myanimelist.net','metadata',1,datetime('now'));
        CREATE TABLE IF NOT EXISTS favorites (
            user_id TEXT,
            manga_id TEXT,
            added_at TEXT,
            PRIMARY KEY (user_id, manga_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (manga_id) REFERENCES manga(id)
        );
        CREATE TABLE IF NOT EXISTS followed_manga (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            source TEXT,
            source_id TEXT,
            title TEXT,
            cover TEXT,
            last_chapter_count INTEGER DEFAULT 0,
            added_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """)

def migrate_db():
    db = get_db()
    def add_col(table, col, type_def, default=None):
        cols = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            clause = f"{col} {type_def}"
            if default is not None:
                clause += f" DEFAULT {default}"
            db.execute(f"ALTER TABLE {table} ADD COLUMN {clause}")
    add_col("manga", "total_chapters", "INTEGER", "0")
    add_col("manga", "status", "TEXT", "'local'")
    add_col("manga", "author", "TEXT", "NULL")
    add_col("manga", "artist", "TEXT", "NULL")
    add_col("manga", "genre", "TEXT", "NULL")
    add_col("manga", "summary", "TEXT", "NULL")
    add_col("manga", "publisher", "TEXT", "NULL")
    add_col("manga", "year", "INTEGER", "NULL")
    add_col("manga", "source", "TEXT", "NULL")
    add_col("manga", "source_id", "TEXT", "NULL")
    add_col("manga", "cover", "TEXT", "NULL")
    add_col("users", "avatar", "TEXT", "NULL")
    add_col("users", "display_name", "TEXT", "NULL")
    add_col("chapters", "volume_id", "TEXT", "NULL")
    add_col("manga", "slug", "TEXT", "NULL")
    db.commit()
    db.close()

init_db()
migrate_db()

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

def is_first_launch():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    db.close()
    return count["cnt"] == 0


SESSION_SECRET = os.environ.get("SECRET_KEY", "change-this-to-a-long-random-string")

def create_session_token(user_id: str, username: str, role: str, display_name: str = None, avatar: str = None) -> str:
    payload = base64.b64encode(json.dumps({"uid": user_id, "username": username, "role": role, "display_name": display_name, "avatar": avatar}).encode()).decode()
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
    if get_scan_setting("scan_on_folder_change") != "1":
        return
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

def start_followed_updates_checker():
    def check_loop():
        while True:
            interval = int(get_scan_setting("scan_interval") or 300)
            time.sleep(interval)
            db = get_db()
            followed = db.execute("SELECT * FROM followed_manga WHERE user_id IS NOT NULL").fetchall()
            db.close()
            for item in followed:
                source = item["source"]
                source_id = item["source_id"]
                try:
                    if source == "mangadex":
                        async def check_mangadex():
                            async with aiohttp.ClientSession() as session:
                                url = f"https://api.mangadex.org/manga/{source_id}/feed?translatedLanguage[]=en&order[chapter]=desc&limit=1"
                                async with session.get(url) as resp:
                                    data = await resp.json()
                            return len(data.get("data", [])) if data.get("data") else item["last_chapter_count"]
                        new_count = asyncio.run(check_mangadex())
                        if new_count > item["last_chapter_count"]:
                            db = get_db()
                            db.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db.commit()
                            db.close()
                    elif source == "mangakakalot":
                        async def check_mangakakalot():
                            from bs4 import BeautifulSoup
                            url = f"https://mangakakalot.com{source_id}" if not source_id.startswith("http") else source_id
                            headers = {"User-Agent": "Mozilla/5.0"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    html = await resp.text()
                            soup = BeautifulSoup(html, "html.parser")
                            chapters = soup.select("ul.row-content-chapter li")
                            return len(chapters)
                        new_count = asyncio.run(check_mangakakalot())
                        if new_count > item["last_chapter_count"]:
                            db = get_db()
                            db.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db.commit()
                            db.close()
                    elif source == "mangafox":
                        async def check_mangafox():
                            from bs4 import BeautifulSoup
                            url = f"https://fanfox.net{source_id}" if not source_id.startswith("http") else source_id
                            headers = {"User-Agent": "Mozilla/5.0"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    html = await resp.text()
                            soup = BeautifulSoup(html, "html.parser")
                            chapters = soup.select(".detail-main-list li")
                            return len(chapters)
                        new_count = asyncio.run(check_mangafox())
                        if new_count > item["last_chapter_count"]:
                            db = get_db()
                            db.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db.commit()
                            db.close()
                except Exception:
                    pass
    threading.Thread(target=check_loop, daemon=True).start()

start_followed_updates_checker()

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

def extract_metadata(file_path: Path) -> dict:
    """Extract metadata from ComicInfo.xml in CBZ/CBR or PDF metadata."""
    suffix = file_path.suffix.lower()
    meta = {}

    try:
        if suffix in ('.cbz', '.zip'):
            with zipfile.ZipFile(file_path) as z:
                names = z.namelist()
                comic_info = next((n for n in names if n.lower().endswith('comicinfo.xml')), None)
                if comic_info:
                    with z.open(comic_info) as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        for tag in ['Series', 'Writer', 'Penciller', 'Genre', 'Summary', 'Publisher', 'Year', 'Volume', 'Number']:
                            el = root.find(tag)
                            if el is not None and el.text:
                                meta[tag.lower()] = el.text.strip()
        elif suffix in ('.cbr', '.rar'):
            with rarfile.RarFile(file_path) as r:
                names = r.namelist()
                comic_info = next((n for n in names if n.lower().endswith('comicinfo.xml')), None)
                if comic_info:
                    with r.open(comic_info) as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        for tag in ['Series', 'Writer', 'Penciller', 'Genre', 'Summary', 'Publisher', 'Year', 'Volume', 'Number']:
                            el = root.find(tag)
                            if el is not None and el.text:
                                meta[tag.lower()] = el.text.strip()
        elif suffix == '.pdf':
            doc = fitz.open(str(file_path))
            pdf_meta = doc.metadata
            if pdf_meta:
                if pdf_meta.get('author'):
                    meta['writer'] = pdf_meta['author']
                if pdf_meta.get('title'):
                    meta['series'] = pdf_meta['title']
                doc.close()
    except:
        pass

    return meta

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
    _set_scan_progress(running=True, total=0, current=0, new_manga=[], message="Scanning...")
    all_items = []
    for dir_conf in directories:
        scan_path = Path(dir_conf["path"])
        if not scan_path.exists():
            continue
        for item in scan_path.iterdir():
            if item.is_dir() or item.suffix.lower() in SUPPORTED_FORMATS:
                all_items.append((scan_path, item))
    _set_scan_progress(total=len(all_items), message="Scanning...")
    db = get_db()
    new_manga_for_meta = []
    for scan_path, item in all_items:
        if item.is_dir():
            manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
            existing = db.execute("SELECT id, slug FROM manga WHERE path=?", (str(item),)).fetchone()
            if not existing:
                chapters = []
                for f in sorted(item.iterdir()):
                    if f.suffix.lower() in SUPPORTED_FORMATS:
                        chapters.append(f)
                if chapters:
                    nfo = read_nfo(item)
                    if nfo and nfo.get("title"):
                        meta = nfo
                    else:
                        meta = {}
                    db.execute(
    """
        INSERT OR IGNORE INTO manga (
            id,
            title,
            slug,
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
            status,
            author,
            artist,
            genre,
            summary,
            publisher,
            year
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            manga_id,
            meta.get('title') or meta.get('series') or item.name,
            slugify(meta.get('title') or meta.get('series') or item.name),
            str(item),
            meta.get('cover'),
            meta.get('total_chapters') or len(chapters),
            0,
            0,
            'single',
            meta.get('source'),
            meta.get('source_id'),
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            meta.get('status') or 'local',
            meta.get('author') or meta.get('writer'),
            meta.get('artist') or meta.get('penciller'),
            meta.get('genre'),
            meta.get('summary'),
            meta.get('publisher'),
            meta.get('year')
        )
    )
                    for i, ch in enumerate(chapters):
                        ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(ch)))
                        ch_num = float(i + 1)
                        db.execute(
                            "INSERT OR IGNORE INTO chapters (id, manga_id, chapter_number, title, path, pages, read_page, is_read, source_url, downloaded, volume_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (ch_id, manga_id, ch_num, ch.stem, str(ch), 0, 0, 0, None, 1, None)
                        )
                    title = meta.get('title') or meta.get('series') or item.name
                    _scan_progress["new_manga"].append({"id": manga_id, "path": str(item), "title": title})
                    if not nfo or not nfo.get("title") or not nfo.get("author"):
                        new_manga_for_meta.append({"id": manga_id, "path": str(item), "title": title})
        elif item.suffix.lower() in SUPPORTED_FORMATS:
            manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
            existing = db.execute("SELECT id FROM manga WHERE path=?", (str(item),)).fetchone()
            if not existing:
                meta = {}
                db.execute("""
INSERT OR IGNORE INTO manga (
    id,
    title,
    slug,
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
    status,
    author,
    artist,
    genre,
    summary,
    publisher,
    year
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    manga_id,
    item.stem,
    slugify(item.stem),
    str(item),
    None,
    1,
    0,
    0,
    'single',
    None,
    None,
    datetime.now().isoformat(),
    datetime.now().isoformat(),
    'local',
    None,
    None,
    None,
    None,
    None,
    None
))
                ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(item)))
                db.execute(
                    "INSERT OR IGNORE INTO chapters (id, manga_id, chapter_number, title, path, pages, read_page, is_read, source_url, downloaded, volume_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ch_id, manga_id, 1.0, item.stem, str(item), 0, 0, 0, None, 1, None)
                )
                _scan_progress["new_manga"].append({"id": manga_id, "path": str(item), "title": item.stem})
                new_manga_for_meta.append({"id": manga_id, "path": str(item), "title": item.stem})
        _set_scan_progress(current=_scan_progress["current"] + 1, message=f"Scanned {_scan_progress['current']}/{_scan_progress['total']}")
    db.commit()
    db.close()
    new_count = len(_scan_progress["new_manga"])
    _set_scan_progress(running=False, message=f"Scan complete. Found {new_count} new series. Fetching metadata..." if new_count else "Scan complete. No new series.")
    for nm in new_manga_for_meta:
        threading.Thread(target=auto_fetch_metadata, args=(nm["id"], nm["path"], nm["title"]), daemon=True).start()
    for nm in _scan_progress["new_manga"]:
        threading.Thread(target=pre_extract_pages, args=(nm["id"],), daemon=True).start()
    _set_scan_progress(running=False, message=f"Scan complete. Found {new_count} new series." if new_count else "Scan complete. No new series.")

def pre_extract_pages(manga_id: str):
    """Pre-extract pages for all chapters of a manga in background."""
    try:
        db = get_db()
        chapters = db.execute("SELECT id, path FROM chapters WHERE manga_id=?", (manga_id,)).fetchall()
        db.close()
        for ch in chapters:
            p = Path(ch["path"])
            if p.exists():
                out_dir = Path("/data/cache") / p.stem
                if not out_dir.exists() or not any(out_dir.iterdir()):
                    extract_pages(p)
    except Exception as e:
        logger.error(f"[pre-extract] Failed for manga {manga_id}: {e}")

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

@app.get("/library")
async def library_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("library.html", {"request": request, "user": user})

@app.get("/favorites")
async def favorites_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("favorites.html", {"request": request, "user": user})

def resolve_manga(identifier: str) -> Optional[str]:
    """Resolve a manga UUID or slug to its ID."""
    db = get_db()
    row = db.execute("SELECT id FROM manga WHERE id=? OR slug=?", (identifier, identifier)).fetchone()
    db.close()
    return row["id"] if row else None

def get_manga_slug(manga_id: str) -> str:
    db = get_db()
    row = db.execute("SELECT slug FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    return row["slug"] if row and row["slug"] else manga_id

@app.get("/manga/{manga_id}")
@app.get("/manga/{manga_id}/{slug:path}")
async def manga_detail(request: Request, manga_id: str, slug: str = None):
    resolved = resolve_manga(manga_id)
    if not resolved:
        raise HTTPException(404, "Manga not found")
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    manga_slug = get_manga_slug(resolved)
    return templates.TemplateResponse("manga_detail.html", {"request": request, "manga_id": resolved, "slug": manga_slug, "user": user})

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
    db.execute(
        "INSERT OR IGNORE INTO user_settings (user_id, reading_mode, strip_scroll_sensitivity, auto_hide_toolbar, show_page_numbers) VALUES (?, ?, ?, ?, ?)",
        (user_row["id"], "single", 1.0, 1, 1)
    )
    db.commit()
    db.close()
    token = create_session_token(user_row["id"], user_row["username"], user_row["role"], user_row["display_name"], user_row["avatar"])
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
    token = create_session_token(user["id"], user["username"], user["role"], user["display_name"], user["avatar"])
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

@app.get("/settings")
async def settings_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("settings.html", {"request": request, "user": user})

@app.get("/read/{manga_id}/{chapter_id}")
@app.get("/read/{manga_id}/{chapter_id}/{slug:path}")
async def reader(request: Request, manga_id: str, chapter_id: str, slug: str = None):
    resolved = resolve_manga(manga_id)
    if not resolved:
        raise HTTPException(404, "Manga not found")
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    manga_slug = get_manga_slug(resolved)
    return templates.TemplateResponse("reader.html", {"request": request, "manga_id": resolved, "slug": manga_slug, "chapter_id": chapter_id, "user": user})

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
    return [dict(r) for r in rows]

@app.get("/api/continue-reading")
async def get_continue_reading():
    db = get_db()
    rows = db.execute("""
        SELECT m.id, m.slug, m.title, m.cover, c.id as chapter_id, c.chapter_number, c.title as chapter_title,
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
    return [dict(r) for r in rows]

# ── Scan Progress Tracking ───────────────────────────────────────────────────

_scan_progress = {"running": False, "total": 0, "current": 0, "new_manga": [], "message": ""}

def _get_scan_progress():
    return dict(_scan_progress)

def _set_scan_progress(**kwargs):
    _scan_progress.update(kwargs)

# ── Routes: API: Scan Settings ────────────────────────────────────────────────

class MangaDirAdd(BaseModel):
    path: str

class ScanSettingsUpdate(BaseModel):
    scan_interval: Optional[int] = None
    auto_scan_enabled: Optional[bool] = None
    watch_enabled: Optional[bool] = None
    scan_on_folder_change: Optional[bool] = None

@app.get("/api/scan-settings")
async def get_scan_settings(request: Request):
    require_admin(request)
    return {
        "scan_interval": int(get_scan_setting("scan_interval") or 300),
        "auto_scan_enabled": get_scan_setting("auto_scan_enabled") == "1",
        "watch_enabled": get_scan_setting("watch_enabled") == "1",
        "scan_on_folder_change": get_scan_setting("scan_on_folder_change") == "1",
        "directories": get_all_manga_directories()
    }

@app.post("/api/scan-settings")
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

@app.post("/api/manga/{manga_id}/favorite")
async def toggle_favorite(manga_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    existing = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id)).fetchone()
    if existing:
        db.execute("DELETE FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id))
        db.commit()
        db.close()
        return {"ok": True, "favorited": False}
    db.execute("INSERT OR IGNORE INTO favorites (user_id, manga_id, added_at) VALUES (?,?,?)", (user["uid"], manga_id, datetime.now().isoformat()))
    db.commit()
    db.close()
    return {"ok": True, "favorited": True}

@app.get("/api/favorites")
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

@app.get("/api/manga/{manga_id}/is-favorite")
async def is_favorite(manga_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    existing = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND manga_id=?", (user["uid"], manga_id)).fetchone()
    db.close()
    return {"favorited": existing is not None}

@app.post("/api/follow")
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
    db.commit()
    db.close()
    return {"ok": True, "followed": True}

@app.post("/api/unfollow/{follow_id}")
async def unfollow_manga(follow_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    db.execute("DELETE FROM followed_manga WHERE id=? AND user_id=?", (follow_id, user["uid"]))
    db.commit()
    db.close()
    return {"ok": True}

@app.get("/api/followed")
async def get_followed(request: Request):
    user = require_auth(request)
    db = get_db()
    rows = db.execute("SELECT * FROM followed_manga WHERE user_id=? ORDER BY added_at DESC", (user["uid"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/check-followed-updates")
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

@app.post("/api/scan-now")
async def scan_now(request: Request):
    require_admin(request)
    if _scan_progress["running"]:
        return {"ok": False, "message": "Scan already running"}
    threading.Thread(target=scan_manga_dir, daemon=True).start()
    return {"ok": True}

@app.get("/api/scan-progress")
async def get_scan_progress(request: Request):
    require_admin(request)
    return _get_scan_progress()

@app.post("/api/scrape-all-covers")
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
                except:
                    pass
        db.commit()
        db.close()
    threading.Thread(target=scrape_covers, daemon=True).start()
    return {"ok": True}

# ── Routes: API: User Settings ────────────────────────────────────────────────

class UserSettingsUpdate(BaseModel):
    reading_mode: Optional[str] = None
    strip_scroll_sensitivity: Optional[float] = None
    auto_hide_toolbar: Optional[bool] = None
    show_page_numbers: Optional[bool] = None
    display_name: Optional[str] = None

@app.get("/api/me")
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

@app.post("/api/me")
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
    db.commit()
    db.close()
    token = create_session_token(user["uid"], user["username"], user["role"], u["display_name"] if u else None, u["avatar"] if u else None)
    response = JSONResponse({"ok": True})
    response.set_cookie(key="session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.post("/api/me/avatar")
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
    token = create_session_token(user["uid"], user["username"], user["role"], u["display_name"] if u else None, u["avatar"] if u else None)
    response = JSONResponse({"ok": True, "avatar": avatar_url})
    response.set_cookie(key="session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

async def download_and_cache_cover(url: str, cache_name: str) -> str:
    if not url or url.startswith("/"):
        return url
    cache_path = Path("/data/cache") / f"{cache_name}.jpg"
    if cache_path.exists():
        return f"/cache/{cache_name}.jpg"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    cache_path.write_bytes(data)
                    return f"/cache/{cache_name}.jpg"
    except:
        pass
    return url

import logging
logger = logging.getLogger("mangashelf")

def auto_fetch_metadata(manga_id: str, manga_path: str, manga_title: str):
    try:
        logger.info(f"[metadata] Fetching for '{manga_title}' ({manga_id})")
        db = get_db()
        manga_row = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
        existing = dict(manga_row) if manga_row else {}
        db.close()
        async def _fetch():
            query = """
            query($search: String, $type: MediaType) {
              Page(perPage: 3) {
                media(search: $search, type: $type, isAdult: false) {
                  id title { romaji english native } coverImage { large medium }
                  status description(asHtml: false) chapters volumes genres format
                  staff { edges { role node { name { full } } } }
                }
              }
            }"""
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://graphql.anilist.co",
                    json={"query": query, "variables": {"search": manga_title, "type": "MANGA"}}
                ) as resp:
                    data = await resp.json()
            media_list = data.get("data", {}).get("Page", {}).get("media", [])
            if not media_list:
                logger.info(f"[metadata] No results for '{manga_title}'")
                return
            best = None
            for m in media_list:
                title = (m["title"].get("english") or m["title"].get("romaji") or "").lower()
                if title and any(word in title for word in manga_title.lower().split()):
                    best = m
                    break
            if not best:
                best = media_list[0]
            title = best["title"].get("english") or best["title"].get("romaji") or best["title"].get("native") or ""
            authors = [e["node"]["name"]["full"] for e in best.get("staff", {}).get("edges", []) if e.get("role") and ("Story" in e["role"] or "Art" in e["role"])]
            cover_url = best.get("coverImage", {}).get("large") or best.get("coverImage", {}).get("medium")
            cached_cover = await download_and_cache_cover(cover_url, f"{manga_id}_cover")
            db = get_db()
            db.execute(
                "UPDATE manga SET title=?, slug=?, author=?, artist=?, genre=?, summary=?, status=?, total_chapters=?, cover=?, updated_at=? WHERE id=?",
                (
                    title,
                    slugify(title),
                    authors[0] if authors else existing.get("author"),
                    authors[1] if len(authors) > 1 else existing.get("artist"),
                    ", ".join(best.get("genres", [])) or existing.get("genre"),
                    (best.get("description") or "")[:2000] if best.get("description") else existing.get("summary"),
                    best.get("status", "").upper() if best.get("status") else existing.get("status"),
                    best.get("chapters") or existing.get("total_chapters", 0),
                    cached_cover,
                    datetime.now().isoformat(),
                    manga_id
                )
            )
            db.commit()
            p = Path(manga_path)
            if p.is_dir():
                existing_nfo = read_nfo(p)
                nfo_data = {
                    "Title": title,
                    "Author": authors[0] if authors else existing.get("author"),
                    "Artist": authors[1] if len(authors) > 1 else existing.get("artist"),
                    "Genre": ", ".join(best.get("genres", [])) or existing.get("genre"),
                    "Summary": (best.get("description") or "")[:2000] if best.get("description") else existing.get("summary"),
                    "Status": best.get("status", "").upper() if best.get("status") else existing.get("status"),
                    "TotalChapters": best.get("chapters") or existing.get("total_chapters", 0),
                    "Cover": cached_cover,
                    "Source": "anilist",
                    "SourceId": str(best["id"])
                }
                for k, v in nfo_data.items():
                    if v is not None and v != "" and v != 0:
                        existing_nfo[k] = v
                write_nfo(p, existing_nfo)
                logger.info(f"[metadata] Wrote NFO for '{title}' at {p}")
            else:
                logger.info(f"[metadata] Skipped NFO write (not a directory): {manga_path}")
            db.close()
            logger.info(f"[metadata] Updated DB for '{title}' ({manga_id})")
        asyncio.run(_fetch())
    except Exception as e:
        logger.error(f"[metadata] Failed for '{manga_title}': {e}")

@app.post("/api/manga/{manga_id}/scrape-cover")
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
                except:
                    pass
        db.close()
    threading.Thread(target=do_scrape, daemon=True).start()
    return {"ok": True}

@app.post("/api/manga/{manga_id}/fetch-metadata")
async def fetch_metadata(manga_id: str, request: Request):
    require_admin(request)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")
    threading.Thread(target=auto_fetch_metadata, args=(manga_id, manga["path"], manga["title"]), daemon=True).start()
    return {"ok": True, "message": f"Fetching metadata for '{manga['title']}'"}

@app.get("/api/manga/{manga_id}")
async def get_manga(manga_id: str, request: Request):
    user = require_auth(request)
    db = get_db()
    manga = db.execute("SELECT * FROM manga WHERE id=?", (manga_id,)).fetchone()
    if not manga:
        db.close()
        raise HTTPException(404, "Manga not found")
    volumes = db.execute("SELECT * FROM volumes WHERE manga_id=? ORDER BY volume_number", (manga_id,)).fetchall()
    chapters = db.execute("SELECT * FROM chapters WHERE manga_id=? ORDER BY chapter_number", (manga_id,)).fetchall()
    progress_rows = db.execute(
        "SELECT c.id, c.is_read, c.read_page, c.pages FROM chapters c WHERE c.manga_id=?", (manga_id,)
    ).fetchall()
    db.close()
    progress = {r["id"]: {"is_read": r["is_read"], "page": r["read_page"], "pages": r["pages"]} for r in progress_rows}
    return {
        "manga": dict(manga),
        "volumes": [dict(v) for v in volumes],
        "chapters": [dict(c) for c in chapters],
        "progress": progress
    }

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

# ── Routes: API: Search ───────────────────────────────────────────────────────

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

    if source == "anilist":
        query = """
        query($search: String, $type: MediaType) {
          Page(perPage: 20) {
            media(search: $search, type: $type, isAdult: false) {
              id
              title { romaji english native }
              coverImage { large medium }
              status
              description(asHtml: false)
              chapters
              genres
              format
            }
          }
        }"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"search": q, "type": "MANGA"}}
            ) as resp:
                data = await resp.json()
        results = []
        for m in data.get("data", {}).get("Page", {}).get("media", []):
            title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or "Unknown"
            cover = m.get("coverImage", {}).get("large") or m.get("coverImage", {}).get("medium")
            results.append({
                "id": str(m["id"]),
                "title": title,
                "cover": cover,
                "status": m.get("status", "").upper() if m.get("status") else None,
                "source": "anilist",
                "description": (m.get("description") or "")[:200] if m.get("description") else "",
                "chapters": m.get("chapters"),
                "genres": m.get("genres", [])
            })
        return results

    if source == "myanimelist":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jikan.moe/v4/manga?q={q}&limit=20&sfw=true"
            async with session.get(url) as resp:
                data = await resp.json()
        results = []
        for m in data.get("data", []):
            results.append({
                "id": str(m["mal_id"]),
                "title": m.get("title") or m.get("title_english") or "Unknown",
                "cover": m.get("images", {}).get("jpg", {}).get("large_image_url") or m.get("images", {}).get("jpg", {}).get("image_url"),
                "status": m.get("status", "").upper() if m.get("status") else None,
                "source": "myanimelist",
                "description": (m.get("synopsis") or "")[:200] if m.get("synopsis") else "",
                "chapters": m.get("chapters"),
                "genres": [g["name"] for g in m.get("genres", [])]
            })
        return results

    if source == "mangakakalot":
        async with aiohttp.ClientSession() as session:
            url = f"https://mangakakalot.com/search/story/{q.replace(' ', '_')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        from bs4 import BeautifulSoup
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
                results.append({
                    "id": href,
                    "title": title,
                    "cover": cover,
                    "status": None,
                    "source": "mangakakalot",
                    "description": ""
                })
        return results

    if source == "mangafox":
        async with aiohttp.ClientSession() as session:
            url = f"https://fanfox.net/search?title={q}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        from bs4 import BeautifulSoup
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
                results.append({
                    "id": href,
                    "title": title,
                    "cover": cover,
                    "status": None,
                    "source": "mangafox",
                    "description": ""
                })
        return results
    return []

@app.get("/api/search-all")
async def search_all_sources(q: str):
    async def search_source(source_id, source_type):
        try:
            if source_type == "mangadex":
                async with aiohttp.ClientSession() as session:
                    url = f"https://api.mangadex.org/manga?title={q}&limit=5&includes[]=cover_art&contentRating[]=safe&contentRating[]=suggestive"
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
                        "id": m["id"], "title": title, "cover": cover,
                        "status": attr.get("status"), "source": source_id,
                        "description": next(iter(attr.get("description", {}).values()), "")[:200]
                    })
                return results

            if source_type == "anilist":
                query = """
                query($search: String, $type: MediaType) {
                  Page(perPage: 5) {
                    media(search: $search, type: $type, isAdult: false) {
                      id title { romaji english native } coverImage { large medium }
                      status description(asHtml: false) chapters genres format
                    }
                  }
                }"""
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://graphql.anilist.co",
                        json={"query": query, "variables": {"search": q, "type": "MANGA"}}
                    ) as resp:
                        data = await resp.json()
                results = []
                for m in data.get("data", {}).get("Page", {}).get("media", []):
                    title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or "Unknown"
                    cover = m.get("coverImage", {}).get("large") or m.get("coverImage", {}).get("medium")
                    results.append({
                        "id": str(m["id"]), "title": title, "cover": cover,
                        "status": m.get("status", "").upper() if m.get("status") else None,
                        "source": source_id,
                        "description": (m.get("description") or "")[:200] if m.get("description") else "",
                        "chapters": m.get("chapters"), "genres": m.get("genres", []), "format": m.get("format")
                    })
                return results

            if source_type == "myanimelist":
                async with aiohttp.ClientSession() as session:
                    url = f"https://api.jikan.moe/v4/manga?q={q}&limit=5&sfw=true"
                    async with session.get(url) as resp:
                        data = await resp.json()
                results = []
                for m in data.get("data", []):
                    results.append({
                        "id": str(m["mal_id"]),
                        "title": m.get("title") or m.get("title_english") or "Unknown",
                        "cover": m.get("images", {}).get("jpg", {}).get("large_image_url") or m.get("images", {}).get("jpg", {}).get("image_url"),
                        "status": m.get("status", "").upper() if m.get("status") else None,
                        "source": source_id,
                        "description": (m.get("synopsis") or "")[:200] if m.get("synopsis") else "",
                        "chapters": m.get("chapters"), "genres": [g["name"] for g in m.get("genres", [])]
                    })
                return results
            return []
        except:
            return []

    db = get_db()
    rows = db.execute("SELECT id, type FROM sources WHERE enabled=1 AND type IN ('mangadex','anilist','myanimelist')").fetchall()
    db.close()

    tasks = [search_source(r["id"], r["type"]) for r in rows]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    merged = []
    for result in all_results:
        if isinstance(result, list):
            merged.extend(result)
    return merged

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

    if source == "mangakakalot":
        from bs4 import BeautifulSoup
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
                chapters.append({
                    "id": link.get("href", ""),
                    "chapter": ch_num,
                    "title": ch_text,
                    "pages": 0,
                    "source": "mangakakalot"
                })
        chapters.reverse()
        return chapters

    if source == "mangafox":
        from bs4 import BeautifulSoup
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
                chapters.append({
                    "id": link.get("href", ""),
                    "chapter": ch_num,
                    "title": ch_text,
                    "pages": 0,
                    "source": "mangafox"
                })
        return chapters

    if source == "anilist":
        return []

    if source == "myanimelist":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jikan.moe/v4/manga/{manga_id}"
            async with session.get(url) as resp:
                data = await resp.json()
        m = data.get("data", {})
        return [{
            "id": "info",
            "chapter": "1",
            "title": f"{m.get('title', '')} — Ch.1–{m.get('chapters', '?')}",
            "pages": 0,
            "source": "myanimelist",
            "metadata": {
                "score": m.get("score"),
                "scored_by": m.get("scored_by"),
                "rank": m.get("rank"),
                "popularity": m.get("popularity"),
                "members": m.get("members"),
                "favorites": m.get("favorites"),
                "volumes": m.get("volumes"),
                "chapters": m.get("chapters"),
                "status": m.get("status"),
                "published": m.get("published", {}).get("string"),
                "authors": [a["name"] for a in m.get("authors", [])],
                "genres": [g["name"] for g in m.get("genres", [])],
                "themes": [t["name"] for t in m.get("themes", [])],
                "serialization": [s["name"] for s in m.get("serialization", [])]
            }
        }]
    return []

@app.get("/api/manga/{manga_id}/metadata")
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
            id
            title { romaji english native }
            coverImage { large medium }
            status
            description(asHtml: false)
            chapters
            volumes
            genres
            synonyms
            format
            startDate { year month day }
            endDate { year month day }
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
                (
                    title,
                    authors[0] if authors else manga["author"],
                    authors[1] if len(authors) > 1 else manga["artist"],
                    ", ".join(m.get("genres", [])),
                    (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
                    m.get("status", "").upper() if m.get("status") else manga["status"],
                    m.get("chapters") or manga["total_chapters"],
                    m.get("startDate", {}).get("year") or manga["year"],
                    ext_cover,
                    datetime.now().isoformat(),
                    manga_id
                )
            )
            db.commit()
            manga_path = Path(manga["path"])
            if manga_path.is_dir():
                write_nfo(manga_path, {
                    "Title": title,
                    "Author": authors[0] if authors else manga["author"],
                    "Artist": authors[1] if len(authors) > 1 else manga["artist"],
                    "Genre": ", ".join(m.get("genres", [])),
                    "Summary": (m.get("description") or "")[:2000] if m.get("description") else manga["summary"],
                    "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
                    "TotalChapters": m.get("chapters") or manga["total_chapters"],
                    "Year": m.get("startDate", {}).get("year") or manga["year"],
                    "Cover": ext_cover,
                    "Source": "anilist",
                    "SourceId": external_id
                })
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
                (
                    m.get("title") or m.get("title_english") or manga["title"],
                    authors[0] if authors else manga["author"],
                    authors[1] if len(authors) > 1 else manga["artist"],
                    ", ".join(genres),
                    (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
                    m.get("status", "").upper() if m.get("status") else manga["status"],
                    m.get("chapters") or manga["total_chapters"],
                    m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
                    (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
                    ext_cover,
                    datetime.now().isoformat(),
                    manga_id
                )
            )
            db.commit()
            manga_path = Path(manga["path"])
            if manga_path.is_dir():
                write_nfo(manga_path, {
                    "Title": m.get("title") or m.get("title_english") or manga["title"],
                    "Author": authors[0] if authors else manga["author"],
                    "Artist": authors[1] if len(authors) > 1 else manga["artist"],
                    "Genre": ", ".join(genres),
                    "Summary": (m.get("synopsis") or "")[:2000] if m.get("synopsis") else manga["summary"],
                    "Status": m.get("status", "").upper() if m.get("status") else manga["status"],
                    "TotalChapters": m.get("chapters") or manga["total_chapters"],
                    "Year": m.get("published", {}).get("prop", {}).get("from", {}).get("year") or manga["year"],
                    "Publisher": (m.get("serialization", [{}])[0].get("name") if m.get("serialization") else manga["publisher"]),
                    "Cover": ext_cover,
                    "Source": "myanimelist",
                    "SourceId": external_id
                })
                _metadata_cache.invalidate(manga_id)
        db.close()
        return {"ok": True, "data": m} if m else {"ok": False, "error": "Not found"}

    db.close()
    raise HTTPException(400, "Invalid source or missing external_id")

@app.get("/api/manga/{manga_id}/nfo")
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
            "title": manga["title"],
            "author": manga["author"],
            "artist": manga["artist"],
            "genre": manga["genre"],
            "summary": manga["summary"],
            "publisher": manga["publisher"],
            "year": manga["year"],
            "status": manga["status"],
            "total_chapters": manga["total_chapters"],
            "cover": manga["cover"],
            "source": manga["source"],
            "source_id": manga["source_id"]
        }
    _metadata_cache.put(manga_id, nfo)
    return nfo

class NfoUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    artist: Optional[str] = None
    genre: Optional[str] = None
    summary: Optional[str] = None
    publisher: Optional[str] = None
    year: Optional[int] = None
    status: Optional[str] = None
    total_chapters: Optional[int] = None
    cover: Optional[str] = None

@app.put("/api/manga/{manga_id}/nfo")
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
            tag = "".join(w.capitalize() if w != "total_chapters" else "TotalChapters" for w in k.split("_"))
            if tag == "TotalChapters":
                tag = "TotalChapters"
            nfo[tag] = v
        existing = read_nfo(manga_path)
        existing.update(nfo)
        write_nfo(manga_path, existing)
    _metadata_cache.invalidate(manga_id)
    return {"ok": True}

@app.get("/api/manga/{manga_id}/find-metadata")
async def find_external_metadata(manga_id: str, source: str = "anilist"):
    db = get_db()
    manga = db.execute("SELECT title FROM manga WHERE id=?", (manga_id,)).fetchone()
    db.close()
    if not manga:
        raise HTTPException(404, "Manga not found")

    if source == "anilist":
        query = """
        query($search: String, $type: MediaType) {
          Page(perPage: 5) {
            media(search: $search, type: $type, isAdult: false) {
              id
              title { romaji english native }
              coverImage { medium }
              status
              chapters
              format
            }
          }
        }"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"search": manga["title"], "type": "MANGA"}}
            ) as resp:
                data = await resp.json()
        results = []
        for m in data.get("data", {}).get("Page", {}).get("media", []):
            title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or "Unknown"
            results.append({
                "id": str(m["id"]),
                "title": title,
                "cover": m.get("coverImage", {}).get("medium"),
                "status": m.get("status", "").upper() if m.get("status") else None,
                "chapters": m.get("chapters"),
                "format": m.get("format")
            })
        return results

    if source == "myanimelist":
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jikan.moe/v4/manga?q={manga['title']}&limit=5&sfw=true"
            async with session.get(url) as resp:
                data = await resp.json()
        results = []
        for m in data.get("data", []):
            results.append({
                "id": str(m["mal_id"]),
                "title": m.get("title") or m.get("title_english") or "Unknown",
                "cover": m.get("images", {}).get("jpg", {}).get("image_url"),
                "status": m.get("status", "").upper() if m.get("status") else None,
                "chapters": m.get("chapters"),
                "volumes": m.get("volumes")
            })
        return results
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

