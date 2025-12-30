import asyncio
import hashlib
import aiofiles
from pathlib import Path
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Tuple

# NEW: The Bypass Library
from curl_cffi.requests import AsyncSession, RequestsError

from .config import (
    BASE_URL,
    HEADERS,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAY,
    CACHE_DIR,
)


class AsyncScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        CACHE_DIR.mkdir(exist_ok=True)

    def clear_cache_for_url(self, url: str):
        """Deletes the cached file for a single URL to free up space."""
        cache_path = Path(self._get_cache_path(url))
        if cache_path.exists():
            cache_path.unlink()

    async def _fetch_url(
        self, session: AsyncSession, url: str
    ) -> Optional[str]:
        """Fetches URL with retry logic and timeout using curl_cffi."""
        for attempt in range(MAX_RETRIES):
            try:
                async with self.semaphore:
                    # curl_cffi: No context manager needed for the request object itself
                    response = await session.get(
                        url, headers=HEADERS, timeout=REQUEST_TIMEOUT
                    )
                    
                    if response.status_code == 429:  # Rate limit
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                        
                    response.raise_for_status()
                    
                    # Note: .text is a property in curl_cffi, not an awaitable method
                    return response.text

            except (RequestsError, asyncio.TimeoutError) as e:
                print(
                    f"⚠️ Network error ({url}): {e}. Retrying {attempt + 1}/{MAX_RETRIES}..."
                )
                await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                print(f"❌ Unexpected Error ({url}): {e}")
                return None

        print(f"❌ Failed to fetch {url} after {MAX_RETRIES} attempts.")
        return None

    def _get_cache_path(self, url: str) -> str:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return str(CACHE_DIR / f"{url_hash}.html")

    async def get_cached_or_fetch(
        self, session: AsyncSession, url: str
    ) -> str:
        cache_path = self._get_cache_path(url)
        path_obj = Path(cache_path)

        if path_obj.exists():
            async with aiofiles.open(cache_path, "r", encoding="utf-8") as f:
                return await f.read()

        content = await self._fetch_url(session, url)
        if content:
            async with aiofiles.open(cache_path, "w", encoding="utf-8") as f:
                await f.write(content)
            return content
        return ""

    def clean_soup(self, html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(
            ["script", "style", "footer", "nav", "aside", "iframe", "div.ads"]
        ):
            tag.decompose()
        return soup

    async def search_novel(self, query: str) -> List[Dict]:
        # Create a local session just for search since it's a standalone action
        async with AsyncSession(impersonate="chrome") as session:
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
            raise ValueError("Could not fetch novel page")

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
        description = ""
        if desc_div:
            paragraphs = [p.get_text(strip=True) for p in desc_div.find_all("p")]
            description = "\n".join(paragraphs)

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
        """
        Checks cache. If missing, downloads, EXTRACTS only the story text,
        and saves that small fragment to cache.
        """
        cache_path = Path(self._get_cache_path(chapter["url"]))

        # 1. Check Cache (Fast Path)
        if cache_path.exists():
            async with aiofiles.open(cache_path, "r", encoding="utf-8") as f:
                return await f.read()

        # 2. Network Request (Slow Path)
        html = await self._fetch_url(session, chapter["url"])
        if not html:
            return None  # Explicit failure signal

        # 3. Parse & Extract IMMEDIATELY
        soup = self.clean_soup(html)
        content_tag = soup.select_one("#chr-content, .chr-c, .chapter-content, article")

        if content_tag:
            # Clean junk tags
            for bad in content_tag.select(
                "div, script, style, .ads, .chapter-title, h3, h4"
            ):
                bad.decompose()

            # Keep only the text, not full HTML
            clean_content = content_tag.decode_contents()

            # 4. Save Optimized Content to Cache
            async with aiofiles.open(cache_path, "w", encoding="utf-8") as f:
                await f.write(clean_content)

            return clean_content

        return None  # Content selector failed
