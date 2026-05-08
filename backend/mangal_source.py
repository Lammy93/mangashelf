import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .utils import logger

MANGAL_BIN = shutil.which("mangal") or shutil.which("mangal.exe")

def mangal_available() -> bool:
    return MANGAL_BIN is not None

def _run(args: list, timeout: int = 60):
    if not MANGAL_BIN:
        raise RuntimeError("mangal CLI not found on PATH")
    try:
        r = subprocess.run(
            [MANGAL_BIN] + args,
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            logger.warning(f"[mangal] stderr: {r.stderr[:500]}")
        return r.stdout, r.stderr
    except FileNotFoundError:
        raise RuntimeError("mangal CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError("mangal command timed out")

async def search_mangal(query: str, limit: int = 20) -> list:
    try:
        stdout, _ = _run(["search", "--json", query], timeout=30)
        data = json.loads(stdout) if stdout.strip() else []
        results = []
        for m in data[:limit]:
            src = m.get("source", {}).get("name", "mangal") if isinstance(m.get("source"), dict) else m.get("source", "mangal")
            results.append({
                "id": m.get("id", ""),
                "title": m.get("name", ""),
                "cover": m.get("coverUrl", "") or m.get("cover", ""),
                "status": m.get("status"),
                "source": f"mangal_{src}",
                "description": m.get("summary", ""),
                "chapters": m.get("chaptersCount"),
                "mangal_source": src,
            })
        return results
    except Exception as e:
        logger.debug(f"[mangal] Search failed: {e}")
        return []

async def get_mangal_chapters(source: str, manga_id: str) -> list:
    try:
        stdout, _ = _run(["chapters", "--json", "-s", source, "-m", manga_id], timeout=30)
        data = json.loads(stdout) if stdout.strip() else []
        chapters = []
        for c in data:
            ch_num = c.get("chapter") or c.get("number") or ""
            chapters.append({
                "id": c.get("id", f"{manga_id}_ch{ch_num}"),
                "chapter": ch_num,
                "title": c.get("title", "") or c.get("name", ""),
                "pages": c.get("pagesCount") or c.get("pages", 0),
            })
        return chapters
    except Exception as e:
        logger.debug(f"[mangal] Chapters failed: {e}")
        return []

async def download_mangal_chapter(manga_title: str, chapter_num: str, source: str, manga_id: str, chapter_id: str, job_id: str):
    from .job_queue import update_job
    try:
        update_job(job_id, status="running", progress=0)
        with tempfile.TemporaryDirectory() as tmpdir:
            _run([
                "inline",
                "--source", source,
                "--query", manga_id,
                "--manga", "first",
                "--chapters", str(chapter_num),
                "-d", tmpdir,
            ], timeout=300)
            out_dir = MANGA_DIR / manga_title
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = Path(tmpdir)
            cbz_files = list(tmp_path.glob("*.cbz")) + list(tmp_path.glob("*.zip"))
            if cbz_files:
                ch_float = float(chapter_num) if chapter_num else 1.0
                dest = out_dir / f"Chapter_{ch_float:06.1f}.cbz"
                shutil.move(str(cbz_files[0]), str(dest))
                update_job(job_id, status="completed", progress=100, completed_at=datetime.now().isoformat())
                from .services import scan_manga_dir
                scan_manga_dir()
            else:
                update_job(job_id, status="failed", error="No CBZ produced by mangal")
    except Exception as e:
        logger.error(f"[mangal] Download failed: {e}")
        update_job(job_id, status="failed", error=str(e))
