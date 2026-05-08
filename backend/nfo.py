import xml.etree.ElementTree as ET
from pathlib import Path

NFO_FILENAME = "mangashelf.xml"

def read_nfo(folder_path: Path) -> dict:
    nfo = folder_path / NFO_FILENAME
    if not nfo.exists():
        return {}
    try:
        tree = ET.parse(str(nfo))
        root = tree.getroot()
        return {
            "title": (root.findtext("Title") or "").strip(),
            "author": (root.findtext("Author") or "").strip(),
            "artist": (root.findtext("Artist") or "").strip(),
            "genre": (root.findtext("Genre") or "").strip(),
            "summary": (root.findtext("Summary") or "").strip(),
            "publisher": (root.findtext("Publisher") or "").strip(),
            "year": int(y) if (y := root.findtext("Year") or "").strip().isdigit() else None,
            "status": (root.findtext("Status") or "").strip(),
            "total_chapters": int(c) if (c := root.findtext("TotalChapters") or "").strip().isdigit() else 0,
            "cover": (root.findtext("Cover") or "").strip(),
            "source_id": (root.findtext("SourceId") or "").strip(),
            "source": (root.findtext("Source") or "").strip(),
        }
    except Exception as e:
        import logging
        logging.getLogger("mangashelf").debug(f"[nfo] Read error: {e}")
        return {}

def write_nfo(folder_path: Path, meta: dict):
    root = ET.Element("MangaShelf")
    TAG_MAP = {
        "title": "Title", "author": "Author", "artist": "Artist",
        "genre": "Genre", "summary": "Summary", "publisher": "Publisher",
        "year": "Year", "status": "Status", "total_chapters": "TotalChapters",
        "cover": "Cover", "source_id": "SourceId", "source": "Source",
    }
    for key, val in meta.items():
        tag = TAG_MAP.get(key, key)
        if val is not None and val != "" and val != 0:
            child = ET.SubElement(root, tag)
            child.text = str(val)
    ET.indent(root)
    nfo = folder_path / NFO_FILENAME
    ET.ElementTree(root).write(str(nfo), encoding="utf-8", xml_declaration=True)
