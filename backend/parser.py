import re
from pathlib import Path

# ── Volume patterns ──────────────────────────────────────────────────────
VOLUME_PATTERNS = [
    re.compile(r'[Vv]ol\.?\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Vv]olume\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Tt]\.\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Tt]ome\s*(\d+(?:\.\d+)?)'),
    re.compile(r'卷\s*(\d+(?:\.\d+)?)'),
    re.compile(r'册\s*(\d+(?:\.\d+)?)'),
    re.compile(r'(\d+(?:\.\d+)?)巻'),
    re.compile(r'(?:^|[\s_.-])[Vv](\d+(?:\.\d+)?)'),  # v1 preceded by separator
]

# ── Chapter patterns ─────────────────────────────────────────────────────
CHAPTER_PATTERNS = [
    re.compile(r'[Cc]h\.?\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Cc]hapter\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Cc]ap(?:ítulo)?\.?\s*(\d+(?:\.\d+)?)'),
    re.compile(r'[Ee]p(?:isodio)?\.?\s*(\d+(?:\.\d+)?)'),
    re.compile(r'#\s*(\d+(?:\.\d+)?)'),
    re.compile(r'第\s*(\d+(?:\.\d+)?)\s*[話话]'),
    re.compile(r'(\d+(?:\.\d+)?)\s*화'),
    re.compile(r'(?<![Vv]\d)[Cc](\d+(?:\.\d+)?)'),  # c001, c1.5
]

# ── Special patterns ─────────────────────────────────────────────────────
SPECIAL_PATTERN = re.compile(r'(?:^|[\s_.-])[Ss][Pp]\s*(\d+)')

# ── Group tag ────────────────────────────────────────────────────────────
GROUP_TAG = re.compile(r'^\[([^\]]+)\]\s*')

# ── Parenthetical stripping ──────────────────────────────────────────────
PAREN_REMOVE = re.compile(r'\([^)]*\)')

# ── Loose number at end ──────────────────────────────────────────────────
LOOSE_NUMBER = re.compile(r'[\s_.-]+(\d+(?:\.\d+)?)\s*$')


class ParsedFile:
    def __init__(self):
        self.series: str = ""
        self.volume: float | None = None
        self.chapter: float | None = None
        self.is_special: bool = False
        self.special_number: int | None = None
        self.year: int | None = None
        self.group: str | None = None

    def __repr__(self):
        parts = [f"series={self.series!r}"]
        if self.volume is not None:
            parts.append(f"vol={self.volume}")
        if self.chapter is not None:
            parts.append(f"ch={self.chapter}")
        if self.is_special:
            parts.append(f"special={self.special_number}")
        if self.group:
            parts.append(f"group={self.group!r}")
        return f"ParsedFile({', '.join(parts)})"


def parse_filename(filename: str, folder_name: str = "") -> ParsedFile:
    stem = Path(filename).stem
    result = ParsedFile()

    if not stem:
        return result

    # 1. Extract [Group] tag from the beginning
    m = GROUP_TAG.match(stem)
    if m:
        result.group = m.group(1)
        stem = stem[m.end():].strip()

    # 2. Strip parenthetical content, but remember year
    year_m = re.search(r'\((\d{4})\)', stem)
    if year_m:
        result.year = int(year_m.group(1))
    stem_clean = PAREN_REMOVE.sub('', stem).strip()
    if stem_clean:
        stem = stem_clean

    # 3. Check for Special marker
    sp_m = SPECIAL_PATTERN.search(stem)
    if sp_m:
        result.is_special = True
        result.special_number = int(sp_m.group(1))
        result.series = _extract_series(stem, folder_name, result)
        return result

    # 4. Extract volume
    vol = _match_first(VOLUME_PATTERNS, stem)
    if vol is not None:
        result.volume = float(vol)

    # 5. Extract chapter
    ch = _match_first(CHAPTER_PATTERNS, stem)
    if ch is not None:
        result.chapter = float(ch)

    # 6. Fallback: if no volume or chapter found, look for loose number
    if result.volume is None and result.chapter is None:
        loose_m = LOOSE_NUMBER.search(stem)
        if loose_m:
            val = float(loose_m.group(1))
            # If above 100, treat as chapter
            if val > 100:
                result.chapter = val
            else:
                result.chapter = val

    # 7. Extract series name from what remains
    result.series = _extract_series(stem, folder_name, result)

    # 8. Clean up series name: remove trailing separators
    result.series = result.series.strip(' -_.')
    if not result.series:
        result.series = folder_name

    return result


def _match_first(patterns: list, text: str) -> str | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _extract_series(stem: str, folder_name: str, parsed: ParsedFile) -> str:
    name = stem

    # Remove volume from name
    if parsed.volume is not None:
        name = _remove_matching(VOLUME_PATTERNS, name)

    # Remove chapter from name
    if parsed.chapter is not None:
        name = _remove_matching(CHAPTER_PATTERNS, name)

    # Remove special marker
    if parsed.is_special:
        name = SPECIAL_PATTERN.sub('', name)

    # Clean up: collapse multiple spaces/hyphens/underscores
    name = re.sub(r'[_.]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*', ' ', name).strip()

    # If nothing left, fall back to folder name
    if not name or len(name) < 2:
        return folder_name

    return name


def _remove_matching(patterns: list, text: str) -> str:
    result = text
    for pat in patterns:
        result = pat.sub('', result)
    return result
