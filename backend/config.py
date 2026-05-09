import os
from pathlib import Path

MANGA_DIR = Path(os.environ.get("MANGA_DIR", "/manga"))
DB_PATH = Path(os.environ.get("DB_PATH", "/data/manga.db"))
SESSION_SECRET = os.environ.get("SECRET_KEY", "change-this-to-a-long-random-string")
AVATAR_DIR = Path("/data/avatars")
CACHE_DIR = Path("/data/cache")
FRONTEND_DIR = Path("/app/frontend")
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"

SUPPORTED_FORMATS = {'.cbz', '.cbr', '.pdf', '.zip', '.rar', '.epub'}
SUPPORTED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
