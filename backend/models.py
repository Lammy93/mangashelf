from pydantic import BaseModel
from typing import Optional

class SourceAdd(BaseModel):
    name: str
    base_url: str

class ReadingProgress(BaseModel):
    chapter_id: str
    page: int

class ReadingModeUpdate(BaseModel):
    mode: str

class MangaDirAdd(BaseModel):
    path: str

class ScanSettingsUpdate(BaseModel):
    scan_interval: Optional[int] = None
    auto_scan_enabled: Optional[bool] = None
    watch_enabled: Optional[bool] = None
    scan_on_folder_change: Optional[bool] = None

class UserSettingsUpdate(BaseModel):
    reading_mode: Optional[str] = None
    strip_scroll_sensitivity: Optional[float] = None
    auto_hide_toolbar: Optional[bool] = None
    show_page_numbers: Optional[bool] = None
    display_name: Optional[str] = None

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

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

class DownloadRequest(BaseModel):
    chapter_id: str
    manga_title: str
    chapter_num: str
    source: str
