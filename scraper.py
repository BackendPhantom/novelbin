import asyncio
import aiofiles
import hashlib
import json
import random
import time  # <--- NEW IMPORT
from pathlib import Path
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Tuple
from curl_cffi.requests import AsyncSession, RequestsError
from http.cookies import SimpleCookie

from .solver import get_headers_automatically
from .config import (
    BASE_URL,
    HEADERS,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAY,
    REQUEST_MIN_INTERVAL,
    REQUEST_MAX_INTERVAL,
    ADAPTIVE_STEP_EVERY,
    ADAPTIVE_INTERVAL_STEP,
    BURST_COOLDOWN_EVERY,
    BURST_COOLDOWN_MIN,
    BURST_COOLDOWN_MAX,
    RECOVERY_REQUESTS_AFTER_CHALLENGE,
    CACHE_DIR,
    COOKIE_FILE,
)
from .utils import generate_slug_variants


class AsyncScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        # <--- NEW: Synchronization Lock
        self.browser_lock = asyncio.Lock()
        self.last_update_time = 0

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        self.current_headers = HEADERS.copy()
        self.current_headers.pop("Cookie", None)
        self.current_cookies: Dict[str, str] = {}
        self.request_guard_lock = asyncio.Lock()
        self.last_request_monotonic = 0.0
        self.min_request_interval = REQUEST_MIN_INTERVAL
        self.max_request_interval = REQUEST_MAX_INTERVAL
        self.total_successful_requests = 0
        self.recovery_requests_left = 0
        self.load_cookies()

    def _effective_request_interval(self) -> float:
        """Adaptive interval grows gradually during long scraping runs."""
        step_count = self.total_successful_requests // max(1, ADAPTIVE_STEP_EVERY)
        adaptive = self.min_request_interval + (step_count * ADAPTIVE_INTERVAL_STEP)

        # After a challenge, temporarily slow down more aggressively.
        if self.recovery_requests_left > 0:
            adaptive += 0.8

        return min(self.max_request_interval, adaptive)

    async def _wait_for_request_slot(self):
        """Global pacing to avoid bot-like burst traffic."""
        async with self.request_guard_lock:
            now = time.monotonic()
            elapsed = now - self.last_request_monotonic
            target_interval = self._effective_request_interval()
            delay = max(0.0, target_interval - elapsed)

            # Small jitter helps avoid perfectly periodic traffic.
            delay += random.uniform(0.05, 0.35)

            if delay > 0:
                await asyncio.sleep(delay)

            self.last_request_monotonic = time.monotonic()

    async def _cooldown_if_needed(self):
        """Occasional longer pause to break suspicious high-volume patterns."""
        if self.total_successful_requests <= 0:
            return

        if BURST_COOLDOWN_EVERY > 0 and (
            self.total_successful_requests % BURST_COOLDOWN_EVERY == 0
        ):
            cooldown = random.uniform(BURST_COOLDOWN_MIN, BURST_COOLDOWN_MAX)
            print(
                f"\n⏸️ Anti-block cooldown: sleeping {cooldown:.1f}s after {self.total_successful_requests} requests..."
            )
            await asyncio.sleep(cooldown)

    def _mark_successful_request(self):
        self.total_successful_requests += 1
        if self.recovery_requests_left > 0:
            self.recovery_requests_left -= 1

    def _looks_like_cloudflare_challenge(self, html: Optional[str]) -> bool:
        if not html:
            return False

        probe = html.lower()
        strong_markers = [
            "just a moment",
            "cf-browser-verification",
            "cdn-cgi/challenge-platform",
            "cf_chl",
            "cf_chl_",
            "checking your browser before accessing",
        ]

        # Avoid false positives from normal pages that merely reference Cloudflare.
        return any(marker in probe for marker in strong_markers)

    def _parse_cookie_string(self, cookie_string: str) -> Dict[str, str]:
        parsed: Dict[str, str] = {}
        if not cookie_string:
            return parsed

        jar = SimpleCookie()
        try:
            jar.load(cookie_string)
            for k, morsel in jar.items():
                parsed[k] = morsel.value
        except Exception:
            # Fallback parser for loosely formatted strings
            for part in cookie_string.split(";"):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k:
                    parsed[k] = v
        return parsed

    def _normalize_solver_payload(
        self, payload: dict
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        Accepts both old and new solver payload formats.
        Returns: (headers, cookies)
        """
        if not isinstance(payload, dict):
            return {}, {}

        # New format: {"headers": {...}, "cookies": {...}}
        if "headers" in payload or "cookies" in payload:
            headers = (payload.get("headers") or {}).copy()
            cookies = (payload.get("cookies") or {}).copy()

            # If solver provided only cookie_string, parse it.
            if not cookies and payload.get("cookie_string"):
                cookies = self._parse_cookie_string(payload.get("cookie_string", ""))

            return headers, cookies

        # Old format: headers dict containing "Cookie"
        headers = payload.copy()
        cookies = {}
        cookie_string = headers.pop("Cookie", None)
        if cookie_string:
            cookies = self._parse_cookie_string(cookie_string)

        return headers, cookies

    def _extract_response_cookies(self, response) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        try:
            resp_cookies = getattr(response, "cookies", None)
            if resp_cookies is None:
                return cookies

            # Common interfaces
            if hasattr(resp_cookies, "items"):
                cookies.update({str(k): str(v) for k, v in resp_cookies.items()})

            if not cookies and hasattr(resp_cookies, "get_dict"):
                cookies.update(
                    {str(k): str(v) for k, v in resp_cookies.get_dict().items()}
                )
        except Exception:
            pass

        return cookies

    def load_cookies(self):
        """Loads cookies/headers from JSON file if it exists."""
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    saved_payload = json.load(f)

                headers, cookies = self._normalize_solver_payload(saved_payload)
                self.current_headers.update(headers)
                self.current_headers.pop("Cookie", None)
                self.current_cookies.update(cookies)

                # Set initialization time so we don't think they are "old" immediately
                self.last_update_time = time.time()
                print("🍪 Loaded saved cookies from disk.")
            except Exception as e:
                print(f"⚠️ Failed to load saved cookies: {e}")

    def save_cookies(self):
        try:
            payload = {
                "headers": self.current_headers,
                "cookies": self.current_cookies,
                "updated_at": self.last_update_time,
            }
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
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

    async def _update_headers_if_needed(self, error_timestamp, prompt_login=False):
        """
        Runs the browser solver safely.
        If multiple tasks fail at once, they queue up here.
        prompt_login: if True, tells the solver to wait for the user to log in.
        """
        # 1. Acquire Lock (Others wait here)
        async with self.browser_lock:

            # 2. The "Double Check":
            # If the cookies were updated AFTER this error occurred,
            # it means a previous task already fixed it. We don't need to do anything.
            if self.last_update_time > error_timestamp:
                return True

            if prompt_login:
                print("\n🔒 Page requires login. Launching browser to log in...")
            else:
                print("\n🔒 403 Forbidden detected. Engaging auto-solver...")

            # 3. Run the solver (blocking call moved to thread)
            new_headers = await asyncio.to_thread(
                get_headers_automatically, prompt_login=prompt_login
            )

            if new_headers:
                headers, cookies = self._normalize_solver_payload(new_headers)
                self.current_headers.update(headers)
                self.current_headers.pop("Cookie", None)

                if cookies:
                    self.current_cookies.update(cookies)

                # Update the timestamp so pending tasks know it's fixed
                self.last_update_time = time.time()
                self.recovery_requests_left = RECOVERY_REQUESTS_AFTER_CHALLENGE
                self.save_cookies()

                print("🔄 Headers updated. Retrying request...")
                return True

            return False

    async def _fetch_url(self, session: AsyncSession, url: str) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                async with self.semaphore:
                    await self._wait_for_request_slot()
                    response = await session.get(
                        url,
                        headers=self.current_headers,
                        cookies=self.current_cookies,
                        timeout=REQUEST_TIMEOUT,
                        impersonate="chrome120",
                    )

                    # Keep cookie jar fresh from response set-cookie headers
                    response_cookies = self._extract_response_cookies(response)
                    if response_cookies:
                        changed = any(
                            self.current_cookies.get(k) != v
                            for k, v in response_cookies.items()
                        )
                        if changed:
                            self.current_cookies.update(response_cookies)
                            self.last_update_time = time.time()
                            self.save_cookies()

                    if response.status_code == 429:
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue

                    if response.status_code in (403, 404):
                        # Both 403 and 404 can mean blocked/not logged in
                        fail_time = time.time()
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )

                        if attempt < MAX_RETRIES:
                            login_hint = response.status_code == 404
                            success = await self._update_headers_if_needed(
                                fail_time, prompt_login=login_hint
                            )
                            if success:
                                await asyncio.sleep(2)
                                continue

                        print("❌ Auto-solver failed or max retries reached.")
                        return None

                    # Some Cloudflare blocks return 200 with challenge HTML.
                    if self._looks_like_cloudflare_challenge(response.text):
                        fail_time = time.time()
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )
                        success = await self._update_headers_if_needed(
                            fail_time, prompt_login=False
                        )
                        if success:
                            await asyncio.sleep(2)
                            continue
                        print("❌ Cloudflare challenge detected and solver failed.")
                        return None

                    response.raise_for_status()
                    self._mark_successful_request()
                    await self._cooldown_if_needed()
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

    async def try_slug_urls(self, query: str) -> Optional[Dict]:
        """
        Fallback: generate slug variants from the query and check if any
        correspond to a valid novel page on novelbin.
        Returns a dict with title and url if found, None otherwise.
        """
        slugs = generate_slug_variants(query)
        if not slugs:
            return None

        async with AsyncSession(impersonate="chrome120") as session:
            for slug in slugs:
                url = f"{BASE_URL}/b/{slug}"
                html = await self._fetch_url(session, url)
                if not html:
                    continue

                soup = self.clean_soup(html)
                title_tag = soup.select_one("h1, .novel-title")
                if title_tag and title_tag.text.strip():
                    return {"title": title_tag.text.strip(), "url": url}

        return None

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
