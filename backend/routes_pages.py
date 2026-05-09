from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from datetime import datetime

from .auth import require_auth, require_admin
from .config import TEMPLATES_DIR
from .db import db_conn, is_first_launch
from .session import verify_session, create_session, delete_session
from .utils import hash_password, verify_password, check_login_rate_limit, generate_id

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

async def get_current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    return verify_session(token)

@router.get("/")
async def index(request: Request):
    if is_first_launch():
        return RedirectResponse(url="/setup")
    user = await get_current_user(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@router.get("/library")
async def library_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("library.html", {"request": request, "user": user})

@router.get("/favorites")
async def favorites_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("favorites.html", {"request": request, "user": user})

@router.get("/manga/{manga_id}")
@router.get("/manga/{manga_id}/{slug:path}")
async def manga_detail(request: Request, manga_id: str, slug: str = None):
    resolved = _resolve_manga(manga_id)
    if not resolved:
        raise HTTPException(404, "Manga not found")
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    manga_slug = _get_manga_slug(resolved)
    return templates.TemplateResponse("manga_detail.html", {"request": request, "manga_id": resolved, "slug": manga_slug, "user": user})

@router.get("/setup")
async def setup_page(request: Request):
    if not is_first_launch():
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("setup.html", {"request": request})

@router.post("/setup")
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
    with db_conn() as db:
        db.execute(
            "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (generate_id(), username, hash_password(password), "admin", datetime.now().isoformat())
        )
        db.commit()
        user_row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO user_settings (user_id, reading_mode, strip_scroll_sensitivity, auto_hide_toolbar, show_page_numbers) VALUES (?, ?, ?, ?, ?)",
            (user_row["id"], "single", 1.0, 1, 1)
        )
        db.commit()
    session_id = create_session(user_row["id"], user_row["username"], user_row["role"], user_row["display_name"], user_row["avatar"])
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session", value=session_id, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@router.get("/login")
async def login_page(request: Request):
    if is_first_launch():
        return RedirectResponse(url="/setup")
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login_post(request: Request):
    if is_first_launch():
        return RedirectResponse(url="/setup")
    client_ip = request.client.host if request.client else "unknown"
    if not check_login_rate_limit(client_ip):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many attempts. Try again later."})
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    with db_conn() as db:
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password."})
    session_id = create_session(user["id"], user["username"], user["role"], user["display_name"], user["avatar"])
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session", value=session_id, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@router.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session")
    if session_id:
        delete_session(session_id)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="session")
    return response

@router.get("/admin")
async def admin_page(request: Request):
    user = require_admin(request)
    return templates.TemplateResponse("admin.html", {"request": request, "user": user})

@router.get("/settings")
async def settings_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("settings.html", {"request": request, "user": user})

@router.get("/read/{manga_id}/{chapter_id}")
@router.get("/read/{manga_id}/{chapter_id}/{slug:path}")
async def reader(request: Request, manga_id: str, chapter_id: str, slug: str = None):
    resolved = _resolve_manga(manga_id)
    if not resolved:
        raise HTTPException(404, "Manga not found")
    ch_resolved = _resolve_chapter(resolved, chapter_id)
    if not ch_resolved:
        raise HTTPException(404, "Chapter not found")
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    manga_slug = _get_manga_slug(resolved)
    return templates.TemplateResponse("reader.html", {"request": request, "manga_id": resolved, "slug": manga_slug, "chapter_id": ch_resolved, "user": user})

@router.get("/read-source/{source}/{manga_id}/{chapter_id}")
async def source_reader(request: Request, source: str, manga_id: str, chapter_id: str, title: str = ""):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("reader.html", {
        "request": request, "manga_id": manga_id, "slug": "",
        "chapter_id": chapter_id, "user": user, "source": source,
        "manga_title": title
    })

@router.get("/sources")
async def sources_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("sources.html", {"request": request, "user": user})

@router.get("/search")
async def search_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("search.html", {"request": request, "user": user})

@router.get("/downloads")
async def downloads_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("downloads.html", {"request": request, "user": user})

# ── Helpers ─────────────────────────────────────────────────────────────

def _resolve_manga(identifier: str):
    with db_conn() as db:
        row = db.execute("SELECT id FROM manga WHERE id=? OR slug=?", (identifier, identifier)).fetchone()
        return row["id"] if row else None

def _resolve_chapter(manga_id: str, identifier: str):
    with db_conn() as db:
        row = db.execute("SELECT id FROM chapters WHERE (id=? OR (manga_id=? AND slug=?))", (identifier, manga_id, identifier)).fetchone()
        return row["id"] if row else None

def _get_manga_slug(manga_id: str) -> str:
    with db_conn() as db:
        row = db.execute("SELECT slug FROM manga WHERE id=?", (manga_id,)).fetchone()
        return row["slug"] if row and row["slug"] else manga_id
