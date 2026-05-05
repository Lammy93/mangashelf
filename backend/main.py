from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import os, json, zipfile, rarfile, fitz, shutil, asyncio, aiohttp, uuid
from pathlib import Path
from datetime import datetime
import sqlite3
import hashlib

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
    """Scan manga directory and update database."""
    db = get_db()
    for item in MANGA_DIR.iterdir():
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
                    # try extract cover
                    try:
                        pages = extract_pages(chapters[0])
                        if pages:
                            cover = pages[0]
                    except:
                        pass
                db.execute(
                    "INSERT OR IGNORE INTO manga VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (manga_id, item.name, str(item), cover, len(chapters), 0, 0, 'single', None, None, datetime.now().isoformat(), datetime.now().isoformat(), 'local')
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
                db.execute(
                    "INSERT OR IGNORE INTO manga VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (manga_id, item.stem, str(item), cover, 1, 0, 0, 'single', None, None, datetime.now().isoformat(), datetime.now().isoformat(), 'local')
                )
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": None
        }
    )

@app.get("/read/{manga_id}/{chapter_id}")
async def reader(request: Request, manga_id: str, chapter_id: str):
    return templates.TemplateResponse("reader.html", {"request": request, "manga_id": manga_id, "chapter_id": chapter_id})

@app.get("/sources")
async def sources_page(request: Request):
    return templates.TemplateResponse("sources.html", {"request": request})

@app.get("/search")
async def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})

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

# ── Cache static serve ────────────────────────────────────────────────────────

from fastapi.responses import FileResponse as FR

@app.get("/cache/{stem}/{filename}")
async def serve_cache(stem: str, filename: str):
    p = Path("/data/cache") / stem / filename
    if not p.exists():
        raise HTTPException(404)
    return FR(str(p))

#  ── Password  ────────────────────────────────────────────────────────
def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_password(pw, hashed):
    return hashlib.sha256(pw.encode()).hexdigest() == hashed