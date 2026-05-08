from abc import ABC, abstractmethod
from typing import Optional
import asyncio
import os
import tempfile
import zipfile
from pathlib import Path

from ..config import MANGA_DIR
from ..utils import logger

_connectors: dict[str, "SourceConnector"] = {}

def register_connector(connector: "SourceConnector"):
    _connectors[connector.id] = connector

def get_connector(source_id: str) -> Optional["SourceConnector"]:
    return _connectors.get(source_id)

def get_all_connectors() -> list["SourceConnector"]:
    return list(_connectors.values())


class SourceConnector(ABC):
    """HakuNeko-style connector for a manga source website.

    Each connector knows how to:
      - search for manga on its source
      - list chapters for a manga
      - extract raw image URLs from a chapter page
      - download and package a chapter into the desired output format
    """

    @property
    @abstractmethod
    def id(self) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def type(self) -> str:
        return self.id

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[dict]:
        ...

    @abstractmethod
    async def get_chapters(self, manga_id: str) -> list[dict]:
        ...

    @abstractmethod
    async def get_page_urls(self, manga_id: str, chapter_id: str) -> list[str]:
        ...

    async def download_chapter(self, chapter_id: str, manga_title: str, chapter_num: str, job_id: str, output_format: str = "cbz"):
        """Download all pages for a chapter and package into the requested format."""
        from ..job_queue import update_job
        try:
            update_job(job_id, status="running", progress=0)
            page_urls = await self.get_page_urls(None, chapter_id)
            if not page_urls:
                raise Exception("No page URLs returned")
            update_job(job_id, status="running", progress=5)

            out_dir = MANGA_DIR / manga_title
            out_dir.mkdir(parents=True, exist_ok=True)

            import aiohttp
            imgs = []
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
                for i, url in enumerate(page_urls):
                    async with s.get(url) as resp:
                        name = url.rsplit("/", 1)[-1].split("?")[0] or f"page_{i:04d}.jpg"
                        imgs.append((name, await resp.read()))
                    pct = 5 + int((i + 1) / len(page_urls) * 70)
                    update_job(job_id, progress=pct)

            ext = output_format.lower()
            if ext == "cbz":
                path = out_dir / f"Chapter_{float(chapter_num):06.1f}.cbz"
                with zipfile.ZipFile(path, "w") as z:
                    for name, data_ in imgs:
                        z.writestr(name, data_)
            elif ext == "pdf":
                path = out_dir / f"Chapter_{float(chapter_num):06.1f}.pdf"
                _make_pdf(imgs, path)
            elif ext == "cbr":
                path = out_dir / f"Chapter_{float(chapter_num):06.1f}.cbr"
                _make_cbr(imgs, path)
            elif ext == "epub":
                path = out_dir / f"Chapter_{float(chapter_num):06.1f}.epub"
                _make_epub(imgs, path, manga_title, chapter_num)
            else:
                path = out_dir / f"Chapter_{float(chapter_num):06.1f}.cbz"
                with zipfile.ZipFile(path, "w") as z:
                    for name, data_ in imgs:
                        z.writestr(name, data_)

            update_job(job_id, status="completed", progress=100)
            from ..services import scan_manga_dir
            scan_manga_dir()
        except Exception as e:
            logger.error(f"[{self.id}] Download failed: {e}")
            update_job(job_id, status="failed", error=str(e))


def _make_pdf(imgs: list, path: Path):
    try:
        import img2pdf
        raw = [data for _, data in imgs]
        pdf = img2pdf.convert(raw)
        path.write_bytes(pdf)
    except ImportError:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(path), pagesize=letter)
        for _, data in imgs:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(data)
                tmpname = tmp.name
            c.drawImage(tmpname, 0, 0, width=letter[0], height=letter[1])
            c.showPage()
            os.unlink(tmpname)
        c.save()


def _make_cbr(imgs: list, path: Path):
    rar_path = path.with_suffix(".rar")
    try:
        import patoolib
        tmpdir = tempfile.mkdtemp()
        for name, data in imgs:
            (Path(tmpdir) / name).write_bytes(data)
        patoolib.create_archive(str(rar_path), [str(Path(tmpdir) / n) for n, _ in imgs])
        import shutil
        os.rename(rar_path, path)
        shutil.rmtree(tmpdir)
    except ImportError:
        with zipfile.ZipFile(path.with_suffix(".cbz"), "w") as z:
            for name, data in imgs:
                z.writestr(name, data)


def _make_epub(imgs: list, path: Path, title: str, chapter: str):
    try:
        from ebooklib import epub
        book = epub.EpubBook()
        book.set_identifier(str(hash(title + chapter)))
        book.set_title(f"{title} - Chapter {chapter}")
        book.set_language("en")
        for i, (name, data) in enumerate(imgs):
            ext = name.rsplit(".", 1)[-1] if "." in name else "jpg"
            img_item = epub.EpubImage()
            img_item.file_name = f"page_{i:04d}.{ext}"
            img_item.media_type = f"image/{ext}" if ext != "jpg" else "image/jpeg"
            img_item.content = data
            book.add_item(img_item)
            page = epub.EpubHtml(title=f"Page {i+1}", file_name=f"page_{i+1}.xhtml", lang="en")
            page.content = f'<html><body><img src="page_{i:04d}.{ext}" style="max-width:100%"/></body></html>'
            book.add_item(page)
            book.toc.append(page)
            book.spine.append(page)
        epub.write_epub(str(path), book)
    except ImportError:
        with zipfile.ZipFile(path.with_suffix(".cbz"), "w") as z:
            for name, data in imgs:
                z.writestr(name, data)
