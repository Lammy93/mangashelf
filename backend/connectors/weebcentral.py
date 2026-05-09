import re
import aiohttp
from bs4 import BeautifulSoup

from .base import SourceConnector
from ..utils import logger


class WeebCentralConnector(SourceConnector):
    id = "weebcentral"
    name = "Weeb Central"

    BASE = "https://weebcentral.com"

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{self.BASE}/search/simple?location=main",
                    data={"text": query},
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            results = []
            for item in soup.select("a")[:limit]:
                href = item.get("href", "")
                if href.startswith(f"{self.BASE}/series/"):
                    title = item.get("title", "") or item.text.strip()
                    img = item.select_one("img")
                    cover = img.get("src", "") if img else ""
                    if cover and not cover.startswith("http"):
                        cover = f"{self.BASE}{cover}"
                    if title and href not in [r["id"] for r in results]:
                        results.append({
                            "id": href,
                            "title": title.strip(),
                            "cover": cover,
                            "status": None,
                            "source": "weebcentral",
                            "description": ""
                        })
            return results[:limit]
        except Exception as e:
            logger.debug(f"[weebcentral] Search failed: {e}")
            return []

    async def get_chapters(self, manga_id: str) -> list[dict]:
        try:
            url = manga_id if manga_id.startswith("http") else f"{self.BASE}/series/{manga_id}"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            chapters = []
            for row in soup.select("#chapter-list > div"):
                link = row.select_one("a[href*='/chapters/']")
                if link:
                    href = link.get("href", "")
                    ch_text = link.get_text(strip=True)
                    ch_num = ch_text
                    m = re.search(r'Chapter\s*([\d.]+)', ch_text, re.I)
                    if m:
                        ch_num = m.group(1)
                    ch_id = href.rsplit("/", 1)[-1] if "/" in href else href
                    chapters.append({
                        "id": ch_id,
                        "chapter": ch_num,
                        "title": ch_text,
                        "pages": 0,
                        "source": "weebcentral"
                    })
            return chapters
        except Exception as e:
            logger.debug(f"[weebcentral] Chapters failed: {e}")
            return []

    async def get_page_urls(self, manga_id: str | None, chapter_id: str) -> list[str]:
        try:
            url = chapter_id if chapter_id.startswith("http") else f"{self.BASE}/chapters/{chapter_id}"
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
                async with s.get(url) as resp:
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")

            img_urls = []

            preload = soup.select_one('link[rel="preload"][as="image"]')
            if preload:
                img_urls.append(preload.get("href", ""))

            if not img_urls:
                for img in soup.select("img.chapter-page, .reader-container img"):
                    src = img.get("src", "") or img.get("data-src", "")
                    if src:
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

            if not img_urls:
                page_btns = soup.select('[id="page_select_modal"] .btn, .page-selector button, [x-data="singlePageNavigation"] [x-text*="page"]')
                total_pages = 0
                for btn in page_btns:
                    text = btn.get_text(strip=True)
                    if text.isdigit():
                        total_pages = max(total_pages, int(text))

                if preload and total_pages > 0:
                    first_url = preload.get("href", "")
                    base = re.sub(r'-\d{3}\.[a-zA-Z]+$', '', first_url)
                    ext = first_url.rsplit(".", 1)[-1] if "." in first_url else "png"
                    for p in range(1, total_pages + 1):
                        padded = f"{p:03d}"
                        url = f"{base}-{padded}.{ext}"
                        if url not in img_urls:
                            img_urls.append(url)

            return img_urls
        except Exception as e:
            logger.debug(f"[weebcentral] Get page URLs failed: {e}")
            return []
