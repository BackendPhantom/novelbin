# library.py
import json
import aiofiles
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

    def add_novel(self, title, url, total_chapters):
        """Adds or updates a novel in the library."""
        self.load()
        self.library[url] = {"title": title, "url": url, "last_count": total_chapters}
        self.save()
        print(f"✅ Added/Updated '{title}' in your library.")

    def get_all(self):
        self.load()
        return self.library
