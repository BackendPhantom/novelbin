# config.py
from pathlib import Path
import os

# Network Settings
CF_CLEARANCE = "nYdglypZ_12iaoSO5fnJoPLooZCqqXVW3TF_fbBzTiY-1767084406-1.2.1.1-AXTVTz.AO2xYObmt4RB_8ypka1 mUkYmZoqeskUvMe0WGjxeEmZRF4sC.zraSPVFL19 5ovSq_7ppA3dGnynmNpkNnoCw7apUdMEY21gaTw VoVDCrECveUeNcv8wsXHy4r7XH4StrR68qlm705xn DEt14iCwejPedG0AT9ApPCMoZW9ih3ePTCsfzPjQ2 ceXOZaZN3cluBNxt3Y0xTv88Vh4jX5OTSZNq6eCCY SRMc3MTJQ1IStqf051KL4w7_LZ4n"
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
BASE_URL = "https://novelbin.com"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://novelbin.com/",
}

MAX_CONCURRENT_REQUESTS = 5  # Limit concurrent connections to avoid bans
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 2  # Seconds


# File Settings
# Default output directory (Android/Termux friendly, but cross-platform)
if "TERMUX_VERSION" in os.environ or Path("/data/data/com.termux").exists():
    DEFAULT_OUTPUT_DIR = Path("/storage/emulated/0/Documents/Novels/New")
    # Local cache to resume downloads
else:
    DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Novels" / "New"

CACHE_DIR = DEFAULT_OUTPUT_DIR / ".cache"

DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_FILE = DEFAULT_OUTPUT_DIR / "library.json"

CACHE_DIR.mkdir(parents=True, exist_ok=True)


# EPUB Styling
CSS_STYLE = """
@namespace epub "http://www.idpf.org/2007/ops";
body {
    font-family: serif;
    line-height: 1.5;
    margin: 0;
    padding: 0 1em;
}
h2 {
    text-align: center;
    margin-top: 2em;
    margin-bottom: 1em;
    border-bottom: 1px solid #ccc;
}
p {
    margin-bottom: 1em;
    text-indent: 1em;
}
"""
