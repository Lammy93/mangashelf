import aiohttp

from .base import SourceConnector
from ..utils import logger


class MangaDexConnector(SourceConnector):
    id = "mangadex"
    name = "MangaDex"

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga?title={query}&limit={limit}&includes[]=cover_art&contentRating[]=safe&contentRating[]=suggestive"
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

    async def get_chapters(self, manga_id: str) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/manga/{manga_id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100"
            async with session.get(url) as resp:
                data = await resp.json()
        chapters = []
        for ch in data.get("data", []):
            attr = ch["attributes"]
            chapters.append({
                "id": ch["id"], "chapter": attr.get("chapter"),
                "title": attr.get("title") or f"Chapter {attr.get('chapter','')}",
                "pages": attr.get("pages", 0), "source": "mangadex"
            })
        return chapters

    async def get_page_urls(self, manga_id: str | None, chapter_id: str) -> list[str]:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mangadex.org/at-home/server/{chapter_id}"
            async with session.get(url) as resp:
                data = await resp.json()
        base = data["baseUrl"]
        hash_ = data["chapter"]["hash"]
        pages = data["chapter"]["data"]
        return [f"{base}/data/{hash_}/{p}" for p in pages]
