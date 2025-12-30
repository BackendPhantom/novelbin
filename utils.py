# utils.py
import re
from typing import List, Set


def clean_filename(text: str) -> str:
    """Sanitizes strings for use in filenames."""
    return re.sub(r'[\\/*:?"<>|]', "", text)


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
