import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .config import DB_PATH

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            slug TEXT,
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
            slug TEXT,
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
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
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

def slugify(text: str) -> str:
    import re
    s = text.lower().strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[-\s]+', '-', s)
    return s[:80]

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
    add_col("chapters", "slug", "TEXT", "NULL")
    add_col("manga", "slug", "TEXT", "NULL")
    for row in db.execute("SELECT id, title FROM manga WHERE slug IS NULL AND title IS NOT NULL"):
        db.execute("UPDATE manga SET slug=? WHERE id=?", (slugify(row["title"]), row["id"]))
    for row in db.execute("SELECT id, title FROM chapters WHERE slug IS NULL AND title IS NOT NULL"):
        db.execute("UPDATE chapters SET slug=? WHERE id=?", (slugify(row["title"]), row["id"]))
    db.commit()
    db.close()

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

def is_first_launch():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    db.close()
    return count["cnt"] == 0

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
