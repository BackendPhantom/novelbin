# library.py
import json
from pathlib import Path

from .config import LIBRARY_FILE


class LibraryManager:
    def __init__(self):
        self.file = LIBRARY_FILE
        self.library = {}

    def load(self):
        """Loads the library from the JSON file."""
        if self.file.exists():
            try:
                with open(self.file, "r", encoding="utf-8") as f:
                    self.library = json.load(f)
            except json.JSONDecodeError:
                self.library = {}
        else:
            self.library = {}

    def save(self):
        """Saves current library to JSON."""
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self.library, f, indent=4, ensure_ascii=False)

    def add_novel(
        self,
        title,
        url,
        total_chapters,
        output_path=None,
        output_filename=None,
    ):
        """Adds or updates a novel in the library."""
        self.load()
        existing = self.library.get(url, {})

        entry = {"title": title, "url": url, "last_count": total_chapters}

        resolved_output_path = output_path or existing.get("output_path")
        if resolved_output_path:
            entry["output_path"] = str(resolved_output_path)
            if not output_filename:
                output_filename = Path(resolved_output_path).stem

        resolved_output_filename = output_filename or existing.get("output_filename")
        if resolved_output_filename:
            entry["output_filename"] = resolved_output_filename

        self.library[url] = entry
        self.save()
        print(f"✅ Added/Updated '{title}' in your library.")

    def get_all(self):
        self.load()
        return self.library
