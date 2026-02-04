import asyncio
import aiofiles
import hashlib
import json
import time  # <--- NEW IMPORT
from pathlib import Path
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Tuple
from curl_cffi.requests import AsyncSession, RequestsError

from .solver import get_headers_automatically
from .config import (
    BASE_URL,
    HEADERS,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAY,
    CACHE_DIR,
    COOKIE_FILE,
)


class AsyncScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        # <--- NEW: Synchronization Lock
        self.browser_lock = asyncio.Lock()
        self.last_update_time = 0

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        self.current_headers = HEADERS.copy()
        self.load_cookies()

    def load_cookies(self):
        """Loads cookies/headers from JSON file if it exists."""
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    saved_headers = json.load(f)
                self.current_headers.update(saved_headers)
                # Set initialization time so we don't think they are "old" immediately
                self.last_update_time = time.time()
                print("🍪 Loaded saved cookies from disk.")
            except Exception as e:
                print(f"⚠️ Failed to load saved cookies: {e}")

    def save_cookies(self, new_headers: dict):
        try:
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(new_headers, f, indent=4)
            print("💾 New cookies saved to disk.")
        except Exception as e:
            print(f"⚠️ Failed to save cookies: {e}")

    def clear_cache_for_url(self, url: str):
        cache_path = Path(self._get_cache_path(url))
        if cache_path.exists():
            cache_path.unlink()

    def _get_cache_path(self, url: str) -> str:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return str(CACHE_DIR / f"{url_hash}.html")

    async def _update_headers_if_needed(self, error_timestamp):
        """
        Runs the browser solver safely.
        If multiple tasks fail at once, they queue up here.
        """
        # 1. Acquire Lock (Others wait here)
        async with self.browser_lock:

            # 2. The "Double Check":
            # If the cookies were updated AFTER this error occurred,
            # it means a previous task already fixed it. We don't need to do anything.
            if self.last_update_time > error_timestamp:
                return True

            print("\n🔒 403 Forbidden detected. Engaging auto-solver...")

            # 3. Run the solver (blocking call moved to thread)
            new_headers = await asyncio.to_thread(get_headers_automatically)

            if new_headers:
                self.current_headers.update(new_headers)
                self.save_cookies(self.current_headers)

                # Update the timestamp so pending tasks know it's fixed
                self.last_update_time = time.time()

                print("🔄 Headers updated. Retrying request...")
                return True

            return False

    async def _fetch_url(self, session: AsyncSession, url: str) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                async with self.semaphore:
                    response = await session.get(
                        url,
                        headers=self.current_headers,
                        timeout=REQUEST_TIMEOUT,
                        impersonate="chrome120",
                    )

                    if response.status_code == 429:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue

                    if response.status_code == 403:
                        # Capture the time of failure
                        fail_time = time.time()

                        if attempt < MAX_RETRIES:
                            # Pass the failure time to the lock manager
                            success = await self._update_headers_if_needed(fail_time)
                            if success:
                                await asyncio.sleep(2)
                                continue

                        print("❌ Auto-solver failed or max retries reached.")
                        return None

                    response.raise_for_status()
                    return response.text

            except (RequestsError, asyncio.TimeoutError) as e:
                print(f"⚠️ Network error ({url}): {e}. Retrying...")
                await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                print(f"❌ Error fetching {url}: {e}")
                return None

        print(f"❌ Failed to fetch {url} after {MAX_RETRIES} attempts.")
        return None

    # --- (Rest of the class remains exactly the same) ---
    def clean_soup(self, html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(
            ["script", "style", "footer", "nav", "aside", "iframe", "div.ads"]
        ):
            tag.decompose()
        return soup

    async def search_novel(self, query: str) -> List[Dict]:
        async with AsyncSession(impersonate="chrome120") as session:
            url = f"{BASE_URL}/search?keyword={query.replace(' ', '+')}"
            html = await self._fetch_url(session, url)
            if not html:
                return []
            soup = self.clean_soup(html)
            results = []
            for item in soup.select(".list-novel .row, .novel-item"):
                title_tag = item.select_one("h3 a, .novel-title a")
                if title_tag:
                    href = title_tag.get("href")
                    if href and not href.startswith("http"):
                        href = BASE_URL + href
                    results.append({"title": title_tag.text.strip(), "url": href})
            return results

    async def extract_metadata(self, session: AsyncSession, url: str) -> Tuple:
        html = await self._fetch_url(session, url)
        if not html:
            raise ValueError(f"Could not fetch novel page: {url}")
        soup = self.clean_soup(html)
        title_tag = soup.select_one("h1, .novel-title")
        title = title_tag.text.strip() if title_tag else "Untitled"
        author = "Unknown"
        for li in soup.select("ul.info li"):
            if "Author" in li.text:
                author = (
                    li.get_text(strip=True)
                    .replace("Author", "")
                    .replace(":", "")
                    .strip()
                )
                break
        cover_tag = soup.select_one(".book img, .cover img")
        cover_url = (
            cover_tag.get("src") or cover_tag.get("data-src") if cover_tag else None
        )
        desc_div = soup.select_one(".desc-text")
        description = (
            "\n".join([p.get_text(strip=True) for p in desc_div.find_all("p")])
            if desc_div
            else ""
        )
        chapters = []
        novel_id_tag = soup.select_one("#rating[data-novel-id]")
        if novel_id_tag:
            novel_id = novel_id_tag["data-novel-id"]
            ajax_url = f"{BASE_URL}/ajax/chapter-archive?novelId={novel_id}"
            ajax_html = await self._fetch_url(session, ajax_url)
            if ajax_html:
                ajax_soup = BeautifulSoup(ajax_html, "html.parser")
                for li in ajax_soup.select("ul.list-chapter li a"):
                    href = li.get("href")
                    if href and not href.startswith("http"):
                        href = BASE_URL + href
                    chapters.append({"name": li.text.strip(), "url": href})
        if not chapters:
            for a in soup.select(".list-chapter li a, .chapter-list li a"):
                href = a.get("href")
                if href and not href.startswith("http"):
                    href = BASE_URL + href
                chapters.append({"name": a.text.strip(), "url": href})
        return title, author, cover_url, description, chapters

    async def fetch_chapter_content(
        self, session: AsyncSession, chapter: Dict
    ) -> Optional[str]:
        cache_path = Path(self._get_cache_path(chapter["url"]))
        if cache_path.exists():
            async with aiofiles.open(cache_path, "r", encoding="utf-8") as f:
                return await f.read()
        html = await self._fetch_url(session, chapter["url"])
        if not html:
            return None
        soup = self.clean_soup(html)
        content_tag = soup.select_one("#chr-content, .chr-c, .chapter-content, article")
        if content_tag:
            for bad in content_tag.select(
                "div, script, style, .ads, .chapter-title, h3, h4"
            ):
                bad.decompose()
            clean_content = content_tag.decode_contents()
            async with aiofiles.open(cache_path, "w", encoding="utf-8") as f:
                await f.write(clean_content)
            return clean_content
        return None
