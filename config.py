# config.py
from pathlib import Path
import os

# Network Settings
BASE_URL = "https://novelbin.com"

# Default Headers (Fallback)
HEADERS = {
    "Referer": BASE_URL,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

MAX_CONCURRENT_REQUESTS = 3
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 3

# File Settings
if "TERMUX_VERSION" in os.environ or Path("/data/data/com.termux").exists():
    DEFAULT_OUTPUT_DIR = Path("/storage/emulated/0/Documents/Novels/New")
else:
    DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Novels"

CACHE_DIR = DEFAULT_OUTPUT_DIR / ".cache"
LIBRARY_FILE = DEFAULT_OUTPUT_DIR / "library.json"
COOKIE_FILE = DEFAULT_OUTPUT_DIR / "cookies.json"  # <--- NEW FILE PATH

DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# EPUB Styling
CSS_STYLE = """
@namespace epub "http://www.idpf.org/2007/ops";
body { font-family: serif; line-height: 1.5; margin: 0; padding: 0 1em; }
h2 { text-align: center; margin-top: 2em; margin-bottom: 1em; border-bottom: 1px solid #ccc; }
p { margin-bottom: 1em; text-indent: 1em; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
"""
