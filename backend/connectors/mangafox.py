import re
import aiohttp
from bs4 import BeautifulSoup

from .base import SourceConnector
from ..utils import logger


class MangaFoxConnector(SourceConnector):
    id = "mangafox"
    name = "MangaFox"

    BASE = "https://fanfox.net"

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.BASE}/search?title={query}",
                                 headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            results = []
            for item in soup.select("ul.manga-list-4-list > li")[:limit]:
                link = item.select_one("p.manga-list-4-item-title a")
                img = item.select_one("img.manga-list-4-cover")
                if link:
                    title = link.get("title", "") or link.text.strip()
                    href = link.get("href", "")
                    cover = img.get("src", "") if img else ""
                    if cover and not cover.startswith("http"):
                        cover = f"{self.BASE}{cover}"
                    results.append({
                        "id": href, "title": title.strip(),
                        "cover": cover, "status": None,
                        "source": "mangafox", "description": ""
                    })
            return results
        except Exception as e:
            logger.debug(f"[mangafox] Search failed: {e}")
            return []

    async def get_chapters(self, manga_id: str) -> list[dict]:
        try:
            url = manga_id if manga_id.startswith("http") else f"{self.BASE}{manga_id}"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            chapters = []
            for row in soup.select(".detail-main-list li")[:500]:
                link = row.select_one("a")
                if link:
                    ch_text = link.select_one("span").text.strip() if link.select_one("span") else link.text.strip()
                    m = re.search(r'ch\.?([\d.]+)', ch_text, re.I)
                    ch_num = m.group(1) if m else ch_text
                    chapters.append({
                        "id": link.get("href", ""), "chapter": ch_num,
                        "title": ch_text, "pages": 0, "source": "mangafox"
                    })
            return chapters
        except Exception as e:
            logger.debug(f"[mangafox] Chapters failed: {e}")
            return []

    async def get_page_urls(self, manga_id: str | None, chapter_id: str) -> list[str]:
        url = chapter_id if chapter_id.startswith("http") else f"{self.BASE}{chapter_id}"
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        img_urls = []
        for img in soup.select("img#image"):
            src = img.get("src", "")
            if src:
                src = src.strip()
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http"):
                    img_urls.append(src)
        if not img_urls:
            import json
            for script in soup.select("script"):
                text = script.string or ""
                m = re.search(r'imgData\s*=\s*(\[.*?\])\s*;', text, re.DOTALL)
                if m:
                    try:
                        for item in json.loads(m.group(1)):
                            if isinstance(item, dict):
                                u = item.get("url", "")
                                if u and u.startswith("http"):
                                    img_urls.append(u)
                            elif isinstance(item, str) and item.startswith("http"):
                                img_urls.append(item)
                    except Exception:
                        pass
        return img_urls
