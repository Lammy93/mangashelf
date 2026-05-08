import logging
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import MANGA_DIR
from .db import init_db, migrate_db, init_default_directory
from .routes_pages import router as pages_router
from .routes_api import router as api_router
from .services import start_folder_watcher, start_interval_scanner, start_followed_updates_checker, start_auto_downloader
from .job_queue import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mangashelf")

app = FastAPI(title="MangaShelf")

# Initialize database
init_db()
migrate_db()
init_default_directory()

# Mount static files
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")
app.mount("/manga-files", StaticFiles(directory=str(MANGA_DIR)), name="manga-files")
Path("/data/avatars").mkdir(parents=True, exist_ok=True)
app.mount("/avatars", StaticFiles(directory="/data/avatars"), name="avatars")

# Include routers
app.include_router(pages_router)
app.include_router(api_router)

# Start background services
start_folder_watcher()
start_interval_scanner()
start_followed_updates_checker()
start_auto_downloader()
start_worker()

logger.info("MangaShelf started")
