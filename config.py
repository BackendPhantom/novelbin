# config.py
import os
from pathlib import Path

# Network Settings
BASE_URL = "https://novelarrow.com"

# Default Headers (Fallback)
HEADERS = {
    "Referer": BASE_URL,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

MAX_CONCURRENT_REQUESTS = 10  # Increased from 3 for faster parallel downloads
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_MIN_INTERVAL = 0.2  # Reduced from 1.8 (chapter content is less suspicious)
REQUEST_MAX_INTERVAL = 0.6  # Reduced from 3.2
ADAPTIVE_STEP_EVERY = 200  # Increased from 25 (delays add less frequently)
ADAPTIVE_INTERVAL_STEP = 0.05  # Reduced from 0.25 (smaller incremental delays)
BURST_COOLDOWN_EVERY = 500  # Increased from 100 (cooldowns happen much less often)
BURST_COOLDOWN_MIN = 3  # Reduced from 8
BURST_COOLDOWN_MAX = 6  # Reduced from 16
RECOVERY_REQUESTS_AFTER_CHALLENGE = 20
TRANSPORT_FAILURES_BEFORE_HTTPX_FALLBACK = 3

# File Settings
if "TERMUX_VERSION" in os.environ or Path("/data/data/com.termux").exists():
    DEFAULT_OUTPUT_DIR = Path("/storage/emulated/0/Documents/Novels/New")
else:
    DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Novels"

CACHE_DIR = DEFAULT_OUTPUT_DIR / ".cache"
LIBRARY_FILE = DEFAULT_OUTPUT_DIR / "library.json"
COOKIE_FILE = DEFAULT_OUTPUT_DIR / "cookies.json"  # <--- NEW FILE PATH
BROWSER_PROFILE_DIR = DEFAULT_OUTPUT_DIR / ".browser_profile"

DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# EPUB Styling
CSS_STYLE = """
@namespace epub "http://www.idpf.org/2007/ops";

body {
    font-family: "Noto Serif", "Times New Roman", serif;
    line-height: 1.6;
    margin: 0;
    padding: 0.5em;
    color: #333;
}

section[epub|type~="chapter"] {
    display: block;
    page-break-before: always;
}

h1.chapter-title {
    text-align: center;
    font-size: 1.8em;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 1.5em;
    padding-top: 1em;
    padding-bottom: 0.5em;
    border-bottom: 2px solid #ddd;
    word-spacing: normal;
    letter-spacing: 0.01em;
}

h4 {
    text-align: center;
    margin-top: 1.5em;
    margin-bottom: 1em;
    font-size: 1.3em;
    border-bottom: 1px solid #ddd;
}

div.chapter-content {
    display: block;
    margin-top: 1em;
}

p {
    margin-bottom: 0.5em;
    text-indent: 1.2em;
    text-align: justify;
}

p:first-of-type {
    text-indent: 0;
}

p.no-indent {
    text-indent: 0;
}

em, i {
    font-style: italic;
}

strong, b {
    font-weight: bold;
}

img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}

blockquote {
    margin: 1em 1.5em;
    padding: 0.5em;
    border-left: 3px solid #ddd;
    font-style: italic;
}

hr {
    margin: 1em 0;
    border: none;
    border-top: 1px solid #ddd;
    height: 0;
}

.epigraph {
    margin: 1.5em 2em;
    text-align: center;
    font-style: italic;
}

/* Ensure proper spacing at page breaks */
section[epub|type~="chapter"] > h1:first-child {
    margin-top: 0;
}
"""
