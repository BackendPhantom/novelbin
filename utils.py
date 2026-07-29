# utils.py
import re
from typing import List, Set

from .config import BASE_URL


def clean_filename(text: str) -> str:
    """Sanitizes strings for use in filenames."""
    return re.sub(r'[\\/*:?"<>|]', "", text)


def generate_slug_variants(query: str) -> List[str]:
    """
    Generate possible URL slug variations from a search query.
    Returns a list of slugs to try, ordered by likelihood.
    """
    STOP_WORDS = {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "is",
        "it",
    }

    # Normalize: lowercase, strip extra whitespace
    cleaned = re.sub(r"[^\w\s-]", "", query.lower()).strip()
    words = cleaned.split()

    slugs = []

    # 1. Direct slug: "the beginning after the end" -> "the-beginning-after-the-end"
    direct = "-".join(words)
    if direct:
        slugs.append(direct)

    # 2. Without stop words: "beginning-after-end"
    no_stop = [w for w in words if w not in STOP_WORDS]
    if no_stop and "-".join(no_stop) != direct:
        slugs.append("-".join(no_stop))

    # 3. Without leading "the": "beginning-after-the-end"
    if words and words[0] in STOP_WORDS and len(words) > 1:
        without_leading = "-".join(words[1:])
        if without_leading not in slugs:
            slugs.append(without_leading)

    return slugs


def slugify_text(text: str) -> str:
    """Convert free text into a URL-safe NovelBin slug."""
    if not text:
        return ""

    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def build_novelbin_url(text: str) -> str:
    """Build a NovelBin chapter/novel URL from a single input string."""
    if not text:
        return ""

    stripped = text.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return stripped

    slug = slugify_text(stripped)
    return f"{BASE_URL}/novel/{slug}" if slug else ""


def build_novelbin_urls(inputs) -> List[str]:
    """Build NovelBin URLs from one string or many inputs."""
    if isinstance(inputs, str):
        raw_inputs = [
            part.strip() for part in re.split(r"[\n,]+", inputs) if part.strip()
        ]
    else:
        raw_inputs = [str(item).strip() for item in inputs if str(item).strip()]

    urls = []
    for item in raw_inputs:
        url = build_novelbin_url(item)
        if url:
            urls.append(url)
    return urls


def is_relevant_result(query: str, title: str, threshold: float = 0.5) -> bool:
    """
    Check if a search result title is relevant to the query.
    Compares the overlap of words between query and title.
    threshold: minimum fraction of query words that must appear in the title.
    """
    query_words = set(re.sub(r"[^\w\s]", "", query.lower()).split())
    title_words = set(re.sub(r"[^\w\s]", "", title.lower()).split())

    if not query_words:
        return False

    matching = query_words & title_words
    return len(matching) / len(query_words) >= threshold


def parse_chapter_selection(selection: str, total_chapters: int) -> List[int]:
    """Parses a string like '1-10, 15, 20-25' into a list of indices."""
    if not selection.strip():
        return list(range(total_chapters))

    indices: Set[int] = set()

    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                s = int(start) - 1
                e = int(end)
                for i in range(s, e):
                    if 0 <= i < total_chapters:
                        indices.add(i)
            except ValueError:
                continue
        elif part.isdigit():
            i = int(part) - 1
            if 0 <= i < total_chapters:
                indices.add(i)

    return sorted(list(indices))
