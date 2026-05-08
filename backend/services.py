import asyncio
import logging
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import aiohttp
import fitz
import rarfile
from bs4 import BeautifulSoup
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import MANGA_DIR, CACHE_DIR, SUPPORTED_FORMATS, SUPPORTED_IMAGE_EXTS
from .db import get_db, get_scan_setting, set_scan_setting, get_manga_directories, slugify
from .nfo import read_nfo, write_nfo
from .cache import _metadata_cache
from .parser import parse_filename
from .utils import (
    logger, bg_submit, is_manga_processing, mark_manga_processing,
    get_scan_progress, set_scan_progress, generate_id
)

# ── Extraction ──────────────────────────────────────────────────────────

def extract_metadata(file_path: Path) -> dict:
    suffix = file_path.suffix.lower()
    meta = {}
    try:
        if suffix in ('.cbz', '.zip'):
            with zipfile.ZipFile(file_path) as z:
                names = z.namelist()
                comic_info = next((n for n in names if n.lower().endswith('comicinfo.xml')), None)
                if comic_info:
                    with z.open(comic_info) as f:
                        import xml.etree.ElementTree as ET
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
                        import xml.etree.ElementTree as ET
                        tree = ET.parse(f)
                        root = tree.getroot()
                        for tag in ['Series', 'Writer', 'Penciller', 'Genre', 'Summary', 'Publisher', 'Year', 'Volume', 'Number']:
                            el = root.find(tag)
                            if el is not None and el.text:
                                meta[tag.lower()] = el.text.strip()
        elif suffix == '.pdf':
            try:
                doc = fitz.open(str(file_path))
                pdf_meta = doc.metadata
                if pdf_meta:
                    if pdf_meta.get('author'):
                        meta['writer'] = pdf_meta['author']
                    if pdf_meta.get('title'):
                        meta['series'] = pdf_meta['title']
                    doc.close()
            except Exception as e:
                logger.debug(f"[metadata] PDF read error: {e}")
    except Exception as e:
        logger.debug(f"[metadata] Extraction error for {file_path.name}: {e}")
    return meta

def extract_pages(file_path: Path) -> list:
    suffix = file_path.suffix.lower()
    out_dir = CACHE_DIR / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = sorted([f for f in out_dir.iterdir() if f.suffix in SUPPORTED_IMAGE_EXTS])
    if pages:
        return [f"/cache/{file_path.stem}/{p.name}" for p in pages]

    if suffix in ('.cbz', '.zip'):
        with zipfile.ZipFile(file_path) as z:
            imgs = sorted([n for n in z.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
            for img in imgs:
                z.extract(img, out_dir)
    elif suffix in ('.cbr', '.rar'):
        with rarfile.RarFile(file_path) as r:
            imgs = sorted([n for n in r.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS])
            for img in imgs:
                r.extract(img, out_dir)
    elif suffix == '.pdf':
        try:
            doc = fitz.open(str(file_path))
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                pix.save(str(out_dir / f"page_{i:04d}.png"))
            doc.close()
        except Exception as e:
            logger.warning(f"[pages] PDF error for {file_path.name}: {e}")

    pages = sorted([f for f in out_dir.iterdir() if f.suffix in SUPPORTED_IMAGE_EXTS])
    return [f"/cache/{file_path.stem}/{p.name}" for p in pages]

def extract_chapter_bg(path: str):
    stem = Path(path).stem
    try:
        file_path = Path(path)
        suffix = file_path.suffix.lower()
        out_dir = CACHE_DIR / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        if suffix in ('.cbz', '.zip'):
            with zipfile.ZipFile(file_path) as z:
                for img in sorted([n for n in z.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS]):
                    out_path = out_dir / Path(img).name
                    if not out_path.exists():
                        out_path.write_bytes(z.read(img))
        elif suffix in ('.cbr', '.rar'):
            with rarfile.RarFile(file_path) as r:
                for img in sorted([n for n in r.namelist() if Path(n).suffix.lower() in SUPPORTED_IMAGE_EXTS]):
                    out_path = out_dir / Path(img).name
                    if not out_path.exists():
                        out_path.write_bytes(r.read(img))
        elif suffix == '.pdf':
            doc = fitz.open(str(file_path))
            for i in range(len(doc)):
                cached = out_dir / f"page_{i:04d}.png"
                if not cached.exists():
                    doc[i].get_pixmap(dpi=150).save(str(cached))
            doc.close()
    except Exception as e:
        logger.warning(f"[bg-extract] Error extracting {stem}: {e}")


# ── ComicInfo.xml ────────────────────────────────────────────────────────

COMICINFO_TAGS = {
    "Series": "title",
    "Title": "chapter_title",
    "Writer": "author",
    "Penciller": "artist",
    "Genre": "genre",
    "Summary": "summary",
    "Publisher": "publisher",
    "Year": "year",
    "Volume": "volume",
    "Number": "chapter",
    "TotalChapters": "total_chapters",
    "Web": "source_url",
    "Notes": "notes",
}


def write_comicinfo_to_cbz(cbz_path: Path, meta: dict):
    suffix = cbz_path.suffix.lower()
    if suffix not in (".cbz", ".zip"):
        return

    existing_xml = None
    try:
        import io
        import zipfile as zf
        with zf.ZipFile(cbz_path, "r") as z:
            for name in z.namelist():
                if name.lower().endswith("comicinfo.xml"):
                    existing_xml = z.read(name)
                    break
    except Exception:
        pass

    root = ET.Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    if existing_xml:
        try:
            existing_tree = ET.fromstring(existing_xml)
            for child in existing_tree:
                root.append(child)
        except Exception:
            pass

    for xml_tag, meta_key in COMICINFO_TAGS.items():
        val = meta.get(meta_key)
        if val is not None and val != "" and val != 0:
            el = root.find(xml_tag)
            if el is None:
                el = ET.SubElement(root, xml_tag)
            el.text = str(val)

    ET.indent(root)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    try:
        import tempfile
        tmp = cbz_path.with_suffix(cbz_path.suffix + ".tmp")
        with zf.ZipFile(cbz_path, "r") as zin:
            with zf.ZipFile(tmp, "w", zf.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if not item.filename.lower().endswith("comicinfo.xml"):
                        zout.writestr(item, zin.read(item.filename))
                zout.writestr("ComicInfo.xml", xml_bytes)
        tmp.replace(cbz_path)
        logger.debug(f"[comicinfo] Written to {cbz_path.name}")
    except Exception as e:
        logger.warning(f"[comicinfo] Failed to write to {cbz_path.name}: {e}")


# ── Library Scanner ─────────────────────────────────────────────────────

def scan_manga_dir():
    directories = get_manga_directories()
    set_scan_progress(running=True, total=0, current=0, new_manga=[], message="Scanning...")
    try:
        db = get_db()
        now_iso = datetime.now().isoformat()
        all_files = []
        for dir_conf in directories:
            scan_path = Path(dir_conf["path"])
            if not scan_path.exists():
                continue
            for item in scan_path.iterdir():
                if item.is_dir():
                    for f in sorted(item.iterdir()):
                        if f.suffix.lower() in SUPPORTED_FORMATS:
                            all_files.append((scan_path, item, f))
                elif item.suffix.lower() in SUPPORTED_FORMATS:
                    all_files.append((scan_path, None, item))

        set_scan_progress(total=len(all_files), message=f"Scanning {len(all_files)} files...")

        # Group files by folder (preserving per-folder identity; standalone files use parsed series)
        series_map = {}
        for scan_path, folder, file_path in all_files:
            parsed = parse_filename(file_path.name, folder.name if folder else "")
            if folder and folder.is_dir():
                group_key = str(folder)
            else:
                group_key = parsed.series or file_path.stem
            if group_key not in series_map:
                series_map[group_key] = {
                    "folder": folder,
                    "parsed_series": parsed.series or (folder.name if folder else file_path.stem),
                    "files": [],
                }
            series_map[group_key]["files"].append({
                "path": file_path,
                "parsed": parsed,
            })

        new_manga_list = []
        new_manga_for_meta = []
        cover_tasks = []

        for group_key, series_data in series_map.items():
            folder = series_data["folder"]
            files = series_data["files"]
            parsed_series = series_data["parsed_series"]

            # Determine manga root path for DB identity
            if folder and folder.is_dir():
                manga_path = str(folder)
            else:
                manga_path = str(files[0]["path"])

            manga_id = str(uuid.uuid5(uuid.NAMESPACE_URL, manga_path))
            existing_manga = db.execute("SELECT id, title FROM manga WHERE id=?", (manga_id,)).fetchone()
            is_new = existing_manga is None

            if is_new:
                nfo = read_nfo(folder) if folder and folder.is_dir() else {}
                meta = nfo if (nfo and nfo.get("title")) else {}
                title = meta.get("title") or parsed_series
                db.execute("""
                    INSERT OR IGNORE INTO manga (
                        id, title, slug, path, cover, total_chapters,
                        last_read_chapter, last_read_page, reading_mode,
                        source, source_id, added_at, updated_at, status,
                        author, artist, genre, summary, publisher, year
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    manga_id, title, slugify(title), manga_path,
                    meta.get("cover"), meta.get("total_chapters") or len(files),
                    0, 0, "single",
                    meta.get("source"), meta.get("source_id"),
                    now_iso, now_iso,
                    meta.get("status") or "local",
                    meta.get("author") or meta.get("writer"),
                    meta.get("artist") or meta.get("penciller"),
                    meta.get("genre"), meta.get("summary"),
                    meta.get("publisher"), meta.get("year"),
                ))
                if not nfo or not nfo.get("title") or not nfo.get("author"):
                    new_manga_for_meta.append({"id": manga_id, "path": manga_path, "title": title})
                new_manga_list.append({"id": manga_id, "path": manga_path, "title": title})
                cover_tasks.append({"id": manga_id, "path": manga_path})
            else:
                title = existing_manga["title"]

            # Process individual files
            for f in files:
                file_path = f["path"]
                parsed = f["parsed"]

                stat = file_path.stat()
                mtime = stat.st_mtime
                fsize = stat.st_size

                tracked = db.execute(
                    "SELECT last_modified FROM file_scan_tracking WHERE file_path=?",
                    (str(file_path),),
                ).fetchone()

                file_changed = tracked is None or tracked["last_modified"] != mtime

                db.execute(
                    "INSERT OR REPLACE INTO file_scan_tracking (file_path, last_modified, file_size, last_scanned) VALUES (?, ?, ?, ?)",
                    (str(file_path), mtime, fsize, now_iso),
                )

                if not file_changed and not is_new:
                    continue

                ch_num = parsed.chapter if parsed.chapter is not None else 1.0
                ch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(file_path)))
                ch_slug = slugify(file_path.stem)

                volume_id = None
                if parsed.volume is not None and folder and folder.is_dir():
                    vol_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{manga_id}_v{parsed.volume}"))
                    existing_vol = db.execute("SELECT id FROM volumes WHERE id=?", (vol_id,)).fetchone()
                    if not existing_vol:
                        db.execute(
                            "INSERT OR IGNORE INTO volumes (id, manga_id, volume_number, title, path) VALUES (?, ?, ?, ?, ?)",
                            (vol_id, manga_id, parsed.volume, f"Volume {parsed.volume}", str(folder)),
                        )
                    volume_id = vol_id

                existing_ch = db.execute("SELECT id FROM chapters WHERE id=?", (ch_id,)).fetchone()
                if existing_ch:
                    db.execute(
                        "UPDATE chapters SET chapter_number=?, title=?, path=?, volume_id=? WHERE id=?",
                        (ch_num, file_path.stem, str(file_path), volume_id, ch_id),
                    )
                else:
                    db.execute(
                        "INSERT OR IGNORE INTO chapters (id, manga_id, chapter_number, title, path, pages, read_page, is_read, source_url, downloaded, volume_id, slug) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ch_id, manga_id, ch_num, file_path.stem, str(file_path), 0, 0, 0, None, 1, volume_id, ch_slug),
                    )

            # Update total chapter count for the series
            ch_count = db.execute("SELECT COUNT(*) as cnt FROM chapters WHERE manga_id=?", (manga_id,)).fetchone()["cnt"]
            db.execute("UPDATE manga SET total_chapters=?, updated_at=? WHERE id=?", (ch_count, now_iso, manga_id))

            set_scan_progress(
                current=len(new_manga_list),
                message=f"Scanned {title} ({len(files)} files)",
            )

        db.commit()
        db.close()

        new_count = len(new_manga_list)
        set_scan_progress(message=f"Scan complete. Found {new_count} new series. Post-processing...")

        # Parallel cover generation
        if cover_tasks:
            def _gen_cover(mid):
                try:
                    d = get_db()
                    ch = d.execute("SELECT path FROM chapters WHERE manga_id=? ORDER BY chapter_number LIMIT 1", (mid,)).fetchone()
                    if ch:
                        p = Path(ch["path"])
                        if p.exists():
                            pages = extract_pages(p)
                            if pages:
                                d2 = get_db()
                                d2.execute("UPDATE manga SET cover=? WHERE id=?", (pages[0], mid))
                                d2.commit()
                                d2.close()
                    d.close()
                except Exception as e:
                    logger.warning(f"[cover] Failed for {mid}: {e}")

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_gen_cover, [ct["id"] for ct in cover_tasks]))

        # Metadata fetch for new manga without NFO
        for nm in new_manga_for_meta:
            bg_submit(auto_fetch_metadata, nm["id"], nm["path"], nm["title"])

        # Pre-extract pages for new manga
        for nm in new_manga_list:
            bg_submit(pre_extract_pages, nm["id"])

        set_scan_progress(
            message=f"Scan complete. Found {new_count} new series." if new_count else "Scan complete. No new series.",
        )
    except Exception as e:
        logger.error(f"[scan] Failed: {e}")
        set_scan_progress(message=f"Scan failed: {e}")
    finally:
        set_scan_progress(running=False)


def pre_extract_pages(manga_id: str):
    mark_manga_processing(manga_id, True)
    try:
        db = get_db()
        chapters = db.execute("SELECT id, path FROM chapters WHERE manga_id=?", (manga_id,)).fetchall()
        db.close()
        for ch in chapters:
            p = Path(ch["path"])
            if p.exists():
                out_dir = CACHE_DIR / p.stem
                if not out_dir.exists() or not any(out_dir.iterdir()):
                    extract_pages(p)
    except Exception as e:
        logger.error(f"[pre-extract] Failed for manga {manga_id}: {e}")
    finally:
        mark_manga_processing(manga_id, False)

# ── Folder Watcher ──────────────────────────────────────────────────────

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
                        async def _check_md():
                            async with aiohttp.ClientSession() as session:
                                url = f"https://api.mangadex.org/manga/{source_id}/feed?translatedLanguage[]=en&order[chapter]=desc&limit=1"
                                async with session.get(url) as resp:
                                    data = await resp.json()
                            return len(data.get("data", [])) if data.get("data") else item["last_chapter_count"]
                        new_count = asyncio.run(_check_md())
                        if new_count > item["last_chapter_count"]:
                            db_up = get_db()
                            db_up.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db_up.commit()
                            db_up.close()
                    elif source == "mangakakalot":
                        async def _check_mkk():
                            url = f"https://mangakakalot.com{source_id}" if not source_id.startswith("http") else source_id
                            headers = {"User-Agent": "Mozilla/5.0"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    html = await resp.text()
                            soup = BeautifulSoup(html, "html.parser")
                            chapters = soup.select("ul.row-content-chapter li")
                            return len(chapters)
                        new_count = asyncio.run(_check_mkk())
                        if new_count > item["last_chapter_count"]:
                            db_up = get_db()
                            db_up.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db_up.commit()
                            db_up.close()
                    elif source == "mangafox":
                        async def _check_mf():
                            url = f"https://fanfox.net{source_id}" if not source_id.startswith("http") else source_id
                            headers = {"User-Agent": "Mozilla/5.0"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    html = await resp.text()
                            soup = BeautifulSoup(html, "html.parser")
                            chapters = soup.select(".detail-main-list li")
                            return len(chapters)
                        new_count = asyncio.run(_check_mf())
                        if new_count > item["last_chapter_count"]:
                            db_up = get_db()
                            db_up.execute("UPDATE followed_manga SET last_chapter_count=? WHERE id=?", (new_count, item["id"]))
                            db_up.commit()
                            db_up.close()
                except Exception as e:
                    logger.debug(f"[followed] Update check failed for {item.get('title','')}: {e}")
    threading.Thread(target=check_loop, daemon=True).start()


def start_auto_downloader():
    def check_loop():
        while True:
            try:
                time.sleep(3600)
                db = get_db()
                rows = db.execute(
                    "SELECT id, title, source, source_id FROM manga WHERE auto_download=1 AND source IS NOT NULL AND source_id IS NOT NULL"
                ).fetchall()
                db.close()
                for row in rows:
                    try:
                        _check_and_download_new_chapters(row)
                    except Exception as e:
                        logger.debug(f"[auto-dl] Check failed for {row['title']}: {e}")
            except Exception as e:
                logger.error(f"[auto-dl] Loop error: {e}")
    threading.Thread(target=check_loop, daemon=True).start()

def _check_and_download_new_chapters(manga: dict):
    source = manga["source"]
    source_id = manga["source_id"]
    if source == "mangadex":
        import asyncio
        async def _fetch():
            async with aiohttp.ClientSession() as session:
                url = f"https://api.mangadex.org/manga/{source_id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=500"
                async with session.get(url) as resp:
                    data = await resp.json()
            return data.get("data", [])
        chapters = asyncio.run(_fetch())
        db = get_db()
        existing = {r["chapter_number"] for r in db.execute(
            "SELECT chapter_number FROM chapters WHERE manga_id=?", (manga["id"],)
        ).fetchall()}
        from .job_queue import create_job
        for ch in chapters:
            ch_num = ch["attributes"].get("chapter")
            if ch_num and float(ch_num) not in existing:
                ch_id = ch["id"]
                create_job("mangadex", manga["title"], str(ch_num), ch_id)
                logger.info(f"[auto-dl] Queued {manga['title']} ch.{ch_num}")
        db.close()


# ── Metadata ────────────────────────────────────────────────────────────

async def download_and_cache_cover(url: str, cache_name: str) -> str:
    if not url or url.startswith("/"):
        return url
    cache_path = CACHE_DIR / f"{cache_name}.jpg"
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

def auto_fetch_metadata(manga_id: str, manga_path: str, manga_title: str):
    mark_manga_processing(manga_id, True)
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
                    title, slugify(title),
                    authors[0] if authors else existing.get("author"),
                    authors[1] if len(authors) > 1 else existing.get("artist"),
                    ", ".join(best.get("genres", [])) or existing.get("genre"),
                    (best.get("description") or "")[:2000] if best.get("description") else existing.get("summary"),
                    best.get("status", "").upper() if best.get("status") else existing.get("status"),
                    best.get("chapters") or existing.get("total_chapters", 0),
                    cached_cover, datetime.now().isoformat(), manga_id
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

                ch_rows = db.execute("SELECT path FROM chapters WHERE manga_id=?", (manga_id,)).fetchall()
                for ch_row in ch_rows:
                    ch_path = Path(ch_row["path"])
                    if ch_path.suffix.lower() in (".cbz", ".zip"):
                        ch_meta = {
                            "series": title,
                            "author": authors[0] if authors else existing.get("author"),
                            "artist": authors[1] if len(authors) > 1 else existing.get("artist"),
                            "genre": ", ".join(best.get("genres", [])) or existing.get("genre"),
                            "summary": (best.get("description") or "")[:2000] if best.get("description") else existing.get("summary"),
                            "year": best.get("startDate", {}).get("year") or existing.get("year"),
                            "publisher": existing.get("publisher"),
                            "total_chapters": best.get("chapters") or existing.get("total_chapters", 0),
                            "source_url": f"https://anilist.co/manga/{best['id']}",
                        }
                        write_comicinfo_to_cbz(ch_path, ch_meta)
            else:
                ch_rows = db.execute("SELECT path FROM chapters WHERE manga_id=?", (manga_id,)).fetchall()
                for ch_row in ch_rows:
                    ch_path = Path(ch_row["path"])
                    if ch_path.suffix.lower() in (".cbz", ".zip"):
                        ch_meta = {
                            "series": title,
                            "author": authors[0] if authors else existing.get("author"),
                            "artist": authors[1] if len(authors) > 1 else existing.get("artist"),
                            "genre": ", ".join(best.get("genres", [])) or existing.get("genre"),
                            "summary": (best.get("description") or "")[:2000] if best.get("description") else existing.get("summary"),
                            "year": best.get("startDate", {}).get("year") or existing.get("year"),
                            "publisher": existing.get("publisher"),
                            "total_chapters": best.get("chapters") or existing.get("total_chapters", 0),
                            "source_url": f"https://anilist.co/manga/{best['id']}",
                        }
                        write_comicinfo_to_cbz(ch_path, ch_meta)
                logger.info(f"[metadata] Skipped NFO write (not a directory): {manga_path}")
            db.close()
            logger.info(f"[metadata] Updated DB for '{title}' ({manga_id})")

        asyncio.run(_fetch())
    except Exception as e:
        logger.error(f"[metadata] Failed for '{manga_title}': {e}")
    finally:
        mark_manga_processing(manga_id, False)

# ── External API helpers ────────────────────────────────────────────────

async def search_mangadex(q: str, limit: int = 20) -> list:
    async with aiohttp.ClientSession() as session:
        url = f"https://api.mangadex.org/manga?title={q}&limit={limit}&includes[]=cover_art&contentRating[]=safe&contentRating[]=suggestive"
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
            "status": attr.get("status"), "source": "mangadex",
            "description": next(iter(attr.get("description", {}).values()), "")[:200]
        })
    return results

async def search_anilist(q: str, limit: int = 20) -> list:
    query = """
    query($search: String, $type: MediaType, $perPage: Int) {
      Page(perPage: $perPage) {
        media(search: $search, type: $type, isAdult: false) {
          id title { romaji english native } coverImage { large medium }
          status description(asHtml: false) chapters genres format
        }
      }
    }"""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": q, "type": "MANGA", "perPage": limit}}
        ) as resp:
            data = await resp.json()
    results = []
    for m in data.get("data", {}).get("Page", {}).get("media", []):
        title = m["title"].get("english") or m["title"].get("romaji") or m["title"].get("native") or "Unknown"
        cover = m.get("coverImage", {}).get("large") or m.get("coverImage", {}).get("medium")
        results.append({
            "id": str(m["id"]), "title": title, "cover": cover,
            "status": m.get("status", "").upper() if m.get("status") else None,
            "source": "anilist",
            "description": (m.get("description") or "")[:200] if m.get("description") else "",
            "chapters": m.get("chapters"), "genres": m.get("genres", [])
        })
    return results

async def search_myanimelist(q: str, limit: int = 20) -> list:
    async with aiohttp.ClientSession() as session:
        url = f"https://api.jikan.moe/v4/manga?q={q}&limit={limit}&sfw=true"
        async with session.get(url) as resp:
            data = await resp.json()
    results = []
    for m in data.get("data", []):
        results.append({
            "id": str(m["mal_id"]), "title": m.get("title") or m.get("title_english") or "Unknown",
            "cover": m.get("images", {}).get("jpg", {}).get("large_image_url") or m.get("images", {}).get("jpg", {}).get("image_url"),
            "status": m.get("status", "").upper() if m.get("status") else None,
            "source": "myanimelist",
            "description": (m.get("synopsis") or "")[:200] if m.get("synopsis") else "",
            "chapters": m.get("chapters"), "genres": [g["name"] for g in m.get("genres", [])]
        })
    return results

# ── Downloads ───────────────────────────────────────────────────────────

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

async def _download_image_list(img_urls: list, manga_title: str, chapter_num: str, job_id: str):
    try:
        download_status[job_id] = {"status": "downloading", "progress": 0, "error": None}
        out_dir = MANGA_DIR / manga_title
        out_dir.mkdir(parents=True, exist_ok=True)
        ch_float = float(chapter_num) if chapter_num else 1.0
        cbz_path = out_dir / f"Chapter_{ch_float:06.1f}.cbz"
        imgs = []
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            for i, url in enumerate(img_urls):
                async with s.get(url) as r:
                    name = url.rsplit("/", 1)[-1].split("?")[0] or f"page_{i:04d}.jpg"
                    imgs.append((name, await r.read()))
                download_status[job_id]["progress"] = int((i + 1) / len(img_urls) * 100)
        with zipfile.ZipFile(cbz_path, "w") as z:
            for name, data_ in imgs:
                z.writestr(name, data_)
        download_status[job_id] = {"status": "complete", "progress": 100, "error": None}
        scan_manga_dir()
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

async def search_mangasee(q: str, limit: int = 20) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://mangasee123.com/_search.php",
                              json={"search": q},
                              headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}) as resp:
                data = await resp.json()
        results = []
        for m in data[:limit]:
            slug = m.get("i", "")
            title = m.get("s", "")
            cover = f"https://temp.compsci88.com/cover/{slug}.jpg" if slug else ""
            results.append({"id": slug, "title": title, "cover": cover,
                            "status": None, "source": "mangasee", "description": ""})
        return results
    except Exception as e:
        logger.debug(f"[mangasee] Search failed: {e}")
        return []

async def search_batoto(q: str, limit: int = 20) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://battwo.com/search?word={q}",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select(".search-result-item")[:limit]:
            link = item.select_one("a")
            img = item.select_one("img")
            if link:
                title = link.get("title", "") or link.text.strip()
                href = link.get("href", "")
                cover = img.get("src", "") if img else ""
                results.append({"id": href, "title": title, "cover": cover,
                                "status": None, "source": "batoto", "description": ""})
        return results
    except Exception as e:
        logger.debug(f"[batoto] Search failed: {e}")
        return []

async def search_asurascans(q: str, limit: int = 20) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://www.asurascans.com/?s={q}",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("div.bsx")[:limit]:
            link = item.select_one("a")
            img = item.select_one("img")
            if link:
                title = link.get("title", "") or link.text.strip()
                href = link.get("href", "")
                cover = img.get("src", "") if img else ""
                results.append({"id": href, "title": title, "cover": cover,
                                "status": None, "source": "asurascans", "description": ""})
        return results
    except Exception as e:
        logger.debug(f"[asurascans] Search failed: {e}")
        return []

async def search_comick(q: str, limit: int = 20) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.comick.io/search?q={q}&limit={limit}",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                data = await resp.json()
        results = []
        for m in data:
            slug = m.get("slug", "")
            title = m.get("title", "")
            cover = f"https://meo.comick.pics/{m.get('md_covers', [{}])[0].get('b2key', '')}" if m.get("md_covers") else ""
            results.append({"id": slug, "title": title, "cover": cover,
                            "status": None, "source": "comick", "description": m.get("desc", "")[:200]})
        return results
    except Exception as e:
        logger.debug(f"[comick] Search failed: {e}")
        return []

async def search_flamescans(q: str, limit: int = 20) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://flamescans.org/?s={q}",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("div.bs")[:limit]:
            link = item.select_one("a")
            img = item.select_one("img")
            if link:
                title = link.get("title", "") or link.text.strip()
                href = link.get("href", "")
                cover = img.get("src", "") if img else ""
                results.append({"id": href, "title": title, "cover": cover,
                                "status": None, "source": "flamescans", "description": ""})
        return results
    except Exception as e:
        logger.debug(f"[flamescans] Search failed: {e}")
        return []

async def get_mangasee_chapters(slug: str) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://mangasee123.com/manga/{slug}",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for opt in soup.select("select#chapterSelect option"):
            val = opt.get("value", "")
            txt = opt.text.strip()
            import re
            m = re.search(r"Ch\.([\d.]+)", txt)
            ch_num = m.group(1) if m else txt
            chapters.append({"id": f"{slug}/{val}", "chapter": ch_num, "title": txt, "pages": 0, "source": "mangasee"})
        return chapters
    except Exception as e:
        logger.debug(f"[mangasee] Chapters failed: {e}")
        return []

async def get_batoto_chapters(url: str) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for row in soup.select(".chapter-list a") or soup.select("a.chapter-item"):
            href = row.get("href", "")
            txt = row.text.strip() or row.select_one("span") and row.select_one("span").text.strip() or ""
            import re
            m = re.search(r"([\d.]+)", txt)
            ch_num = m.group(1) if m else txt
            chapters.append({"id": href, "chapter": ch_num, "title": txt, "pages": 0, "source": "batoto"})
        return chapters
    except Exception as e:
        logger.debug(f"[batoto] Chapters failed: {e}")
        return []

async def get_asurascans_chapters(url: str) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for item in soup.select("li.wp-manga-chapter a") or soup.select(".chapter-list a"):
            href = item.get("href", "")
            txt = item.text.strip()
            import re
            m = re.search(r"Chapter\s*([\d.]+)", txt, re.I)
            ch_num = m.group(1) if m else txt
            chapters.append({"id": href, "chapter": ch_num, "title": txt, "pages": 0, "source": "asurascans"})
        return chapters
    except Exception as e:
        logger.debug(f"[asurascans] Chapters failed: {e}")
        return []

async def get_comick_chapters(slug: str) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.comick.io/comic/{slug}/chapter?limit=500",
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                data = await resp.json()
        chapters = []
        for ch in data.get("chapters", []):
            ch_num = ch.get("chap", "")
            hid = ch.get("hid", "")
            title = ch.get("title", "") or f"Chapter {ch_num}"
            chapters.append({"id": hid, "chapter": ch_num, "title": title, "pages": ch.get("page_count", 0), "source": "comick"})
        return chapters
    except Exception as e:
        logger.debug(f"[comick] Chapters failed: {e}")
        return []

async def get_flamescans_chapters(url: str) -> list:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for item in soup.select("li.wp-manga-chapter a") or soup.select(".chapter-list a"):
            href = item.get("href", "")
            txt = item.text.strip()
            import re
            m = re.search(r"Chapter\s*([\d.]+)", txt, re.I)
            ch_num = m.group(1) if m else txt
            chapters.append({"id": href, "chapter": ch_num, "title": txt, "pages": 0, "source": "flamescans"})
        return chapters
    except Exception as e:
        logger.debug(f"[flamescans] Chapters failed: {e}")
        return []

# ── Source Downloaders ────────────────────────────────────────────────

async def download_mangasee_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        slug = chapter_id.split("/")[0]
        ch_part = chapter_id.split("/")[1] if "/" in chapter_id else chapter_num
        url = f"https://mangasee123.com/read-online/{slug}-chapter-{ch_part}.html"
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url) as resp:
                html = await resp.text()
        import json, re
        m = re.search(r"vm\.CurChapter\s*=\s*({.*?});", html, re.DOTALL)
        if not m:
            raise Exception("Could not find chapter data")
        ch_data = json.loads(m.group(1))
        pages = ch_data.get("Pages", [])
        host = "https://temp.compsci88.com"
        img_urls = [f"{host}/manga/{slug}/{ch_part}-{str(p).zfill(3)}.png" for p in range(1, len(pages) + 1)]
        await _download_image_list(img_urls, manga_title, chapter_num, job_id)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

async def download_batoto_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(chapter_id) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        img_urls = []
        for img in soup.select("img.img-fluid") or soup.select("img.page-img") or soup.select("#page img"):
            src = img.get("src") or img.get("data-src", "")
            if src and src.startswith("http"):
                img_urls.append(src)
        if not img_urls:
            import json, re
            m = re.search(r"pages\s*=\s*(\[.*?\]);", html, re.DOTALL)
            if m:
                for p in json.loads(m.group(1)):
                    if isinstance(p, str) and p.startswith("http"):
                        img_urls.append(p)
        await _download_image_list(img_urls, manga_title, chapter_num, job_id)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

async def download_asurascans_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(chapter_id) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        img_urls = []
        for img in soup.select(".reading-content img") or soup.select("img.wp-manga-chapter-img"):
            src = img.get("src") or img.get("data-src", "") or img.get("data-lazy-src", "")
            if src.startswith("http"):
                img_urls.append(src)
        await _download_image_list(img_urls, manga_title, chapter_num, job_id)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

async def download_comick_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(f"https://api.comick.io/chapter/{chapter_id}") as resp:
                data = await resp.json()
        pages = data.get("chapter", {}).get("images", []) or data.get("images", []) or data.get("page_urls", [])
        img_urls = []
        for p in pages:
            if isinstance(p, str):
                img_urls.append(f"https://meo.comick.pics/{p}" if not p.startswith("http") else p)
            elif isinstance(p, dict):
                url = p.get("url", p.get("src", ""))
                if url:
                    img_urls.append(f"https://meo.comick.pics/{url}" if not url.startswith("http") else url)
        await _download_image_list(img_urls, manga_title, chapter_num, job_id)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

async def download_flamescans_chapter(chapter_id: str, manga_title: str, chapter_num: str, job_id: str):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(chapter_id) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        img_urls = []
        for img in soup.select(".reading-content img") or soup.select("img.wp-manga-chapter-img"):
            src = img.get("src") or img.get("data-src", "") or img.get("data-lazy-src", "")
            if src.startswith("http"):
                img_urls.append(src)
        await _download_image_list(img_urls, manga_title, chapter_num, job_id)
    except Exception as e:
        download_status[job_id] = {"status": "error", "progress": 0, "error": str(e)}

# ── Job Queue Dispatcher ──────────────────────────────────────────────

def _run_download_job(job: dict):
    source = job["source"]
    if source.startswith("mangal_"):
        mangal_src = source.split("mangal_", 1)[1]
        from .mangal_source import download_mangal_chapter as dl
        import asyncio
        asyncio.run(dl(
            job["manga_title"], job["chapter_number"],
            mangal_src, job["manga_title"], job["chapter_id"], job["id"]
        ))
    else:
        import asyncio
        tasks_map = {
            "mangadex": download_mangadex_chapter,
            "mangasee": download_mangasee_chapter,
            "batoto": download_batoto_chapter,
            "asurascans": download_asurascans_chapter,
            "comick": download_comick_chapter,
            "flamescans": download_flamescans_chapter,
        }
        fn = tasks_map.get(source)
        if fn:
            asyncio.run(fn(job["chapter_id"], job["manga_title"], job["chapter_number"], job["id"]))

async def search_mangal_all(q: str, limit: int = 5) -> list:
    try:
        from .mangal_source import mangal_available, search_mangal
        if not mangal_available():
            return []
        return await search_mangal(q, limit=limit)
    except Exception:
        return []
