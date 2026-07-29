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
    TRANSPORT_FAILURES_BEFORE_HTTPX_FALLBACK,
    CACHE_DIR,
    COOKIE_FILE,
)
from .utils import generate_slug_variants


class AsyncScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        # Limit concurrent file I/O to prevent "too many open files" errors
        self.file_io_semaphore = asyncio.Semaphore(10)

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
        self.last_solver_launch_monotonic = 0.0
        self.solver_cooldown_seconds = 180
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

            # Minimal jitter for faster downloads
            delay += random.uniform(0.01, 0.05)

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

    def _solver_launch_allowed(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_solver_launch_monotonic
        return elapsed >= self.solver_cooldown_seconds

    def _looks_like_cloudflare_challenge(self, html: Optional[str]) -> bool:
        if not html:
            return False

        probe = html.lower()
        # Very specific markers only (avoid false positives from novel text like
        # "just a moment").
        if "cdn-cgi/challenge-platform" in probe:
            return True
        if "cf-browser-verification" in probe:
            return True
        if "cf_chl" in probe or "cf_chl_" in probe:
            return True

        # Fallback: classic interstitial title + cloudflare mention.
        if "<title>just a moment" in probe and "cloudflare" in probe:
            return True

        return False

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

    async def _fetch_url_httpx_fallback(self, url: str) -> Optional[str]:
        """Fallback fetch path for repeated transport/connect failures."""
        try:
            import httpx
        except Exception:
            print(
                "⚠️ httpx fallback unavailable (httpx not installed). "
                "Install with: pip install httpx"
            )
            return None

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                http2=True,
            ) as client:
                response = await client.get(
                    url,
                    headers=self.current_headers,
                    cookies=self.current_cookies,
                )

            response_text = response.text

            # Keep cookie jar fresh from response set-cookie headers
            response_cookies = {str(k): str(v) for k, v in response.cookies.items()}
            if response_cookies:
                changed = any(
                    self.current_cookies.get(k) != v
                    for k, v in response_cookies.items()
                )
                if changed:
                    self.current_cookies.update(response_cookies)
                    self.last_update_time = time.time()
                    self.save_cookies()

            # Do not run solver here; fallback path is for transport reliability only.
            if response.status_code in (403, 404, 429):
                return None

            if self._looks_like_cloudflare_challenge(response_text):
                return None

            response.raise_for_status()
            return response_text
        except Exception as e:
            print(f"⚠️ httpx fallback failed ({url}): {e}")
            return None

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

            self.last_solver_launch_monotonic = time.monotonic()

            # 3. Run the solver (blocking call moved to thread)
            from .solver import get_headers_automatically

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
        transport_error_streak = 0
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
                    response_text = response.text

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

                    if response.status_code == 404:
                        # Real not-found responses should not trigger solver.
                        return None

                    if response.status_code == 403:
                        fail_time = time.time()
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )

                        looks_like_challenge = self._looks_like_cloudflare_challenge(
                            response_text
                        )

                        # If this is a generic 403 without Cloudflare challenge markers,
                        # do a backoff retry and avoid opening browser repeatedly.
                        if not looks_like_challenge:
                            pause = (RETRY_DELAY * (attempt + 1)) + random.uniform(
                                1.0, 3.0
                            )
                            print(
                                f"⚠️ 403 (no challenge markers) for {url}. Backing off {pause:.1f}s..."
                            )
                            await asyncio.sleep(pause)
                            continue

                        if attempt < MAX_RETRIES:
                            # For confirmed challenge pages, bypass cooldown and let
                            # the shared lock+timestamp dedupe solver launches.
                            success = await self._update_headers_if_needed(
                                fail_time, prompt_login=False
                            )

                            if success:
                                await asyncio.sleep(2)
                                continue

                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue

                        print("❌ Auto-solver failed or max retries reached.")
                        return None

                    # Some Cloudflare blocks return 200 with challenge HTML.
                    if self._looks_like_cloudflare_challenge(response_text):
                        fail_time = time.time()
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )

                        # For confirmed challenge pages, bypass cooldown and let
                        # the shared lock+timestamp dedupe solver launches.
                        success = await self._update_headers_if_needed(
                            fail_time, prompt_login=False
                        )

                        if success:
                            await asyncio.sleep(2)
                            continue
                        print("❌ Cloudflare challenge detected and solver failed.")
                        return None

                    response.raise_for_status()
                    transport_error_streak = 0
                    self._mark_successful_request()
                    await self._cooldown_if_needed()
                    return response.text

            except (RequestsError, asyncio.TimeoutError) as e:
                transport_error_streak += 1
                print(f"⚠️ Network error ({url}): {e}. Retrying...")

                # Try httpx fallback earlier during concurrent operations (like update)
                # to avoid cascading failures across parallel calls
                should_try_httpx = (
                    transport_error_streak >= TRANSPORT_FAILURES_BEFORE_HTTPX_FALLBACK
                    or (
                        attempt == MAX_RETRIES - 1
                    )  # Last attempt - try httpx as alternative
                )

                if should_try_httpx:
                    print("🔁 Trying httpx fallback for transport recovery...")
                    fallback_text = await self._fetch_url_httpx_fallback(url)
                    if fallback_text is not None:
                        print("✅ httpx fallback succeeded.")
                        self._mark_successful_request()
                        await self._cooldown_if_needed()
                        return fallback_text

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
        seen_urls = set()
        novel_slug = url.rstrip("/").split("/")[-1]
        chapter_url_marker = f"/b/{novel_slug}/chapter-"

        def add_chapter_links(source_html: str):
            source_soup = BeautifulSoup(source_html, "html.parser")
            for a in source_soup.select("a[href]"):
                href = a.get("href")
                if not href:
                    continue
                if chapter_url_marker not in href:
                    continue
                if not href.startswith("http"):
                    href = BASE_URL + href
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                # Prefer visible text, but fall back to a readable slug-derived title
                name_text = a.get_text(" ", strip=True)
                if not name_text:
                    # Derive from URL slug: .../chapter-1-title-goes-here
                    slug = href.rstrip("/\n\r ").split("/")[-1]
                    if slug.startswith("chapter-"):
                        slug_title = slug[len("chapter-") :]
                    else:
                        slug_title = slug
                    # Replace hyphens with spaces and collapse multiple spaces
                    derived = " ".join(
                        part for part in slug_title.replace("-", " ").split()
                    )
                    # Capitalize first letter to make it look nicer
                    name_text = derived.capitalize() if derived else slug

                chapters.append({"name": name_text, "url": href})

        novel_id_tag = soup.select_one("#rating[data-novel-id]")
        if novel_id_tag:
            novel_id = novel_id_tag["data-novel-id"]
            ajax_url = f"{BASE_URL}/ajax/chapter-archive?novelId={novel_id}"
            ajax_html = await self._fetch_url(session, ajax_url)
            if ajax_html:
                add_chapter_links(ajax_html)

        add_chapter_links(html)
        return title, author, cover_url, description, chapters

    async def fetch_chapter_content(
        self, session: AsyncSession, chapter: Dict
    ) -> Optional[Dict[str, str]]:
        cache_path = Path(self._get_cache_path(chapter["url"]))

        # Use semaphore to limit concurrent file I/O operations
        async with self.file_io_semaphore:
            if cache_path.exists():
                try:
                    async with aiofiles.open(cache_path, "r", encoding="utf-8") as f:
                        return {
                            "title": chapter.get("name", ""),
                            "content": await f.read(),
                        }
                except Exception:
                    pass  # Fall back to fetching if cache read fails

        html = await self._fetch_url(session, chapter["url"])
        if not html:
            return None
        soup = self.clean_soup(html)

        # Extract chapter title: first try h4 in #chr-content, then span.chr-text
        chapter_title = ""
        chr_content = soup.select_one("#chr-content")
        if chr_content:
            h4_tag = chr_content.find("h4")
            if h4_tag:
                chapter_title = h4_tag.get_text(strip=True)

        if not chapter_title:
            span_tag = soup.select_one("span.chr-text")
            if span_tag:
                chapter_title = span_tag.get_text(strip=True)

        if not chapter_title:
            chapter_title = chapter.get("name", "")

        # Try a list of common selectors in order of likelihood
        selectors = [
            "#chr-content",
            ".chr-c",
            ".chapter-content",
            "article",
            "#chapter",
            ".read-content",
            ".content",
            ".chapter-inner",
        ]

        content_tag = None
        for sel in selectors:
            content_tag = soup.select_one(sel)
            if content_tag:
                break

        # If still not found, check for noscript blocks that sometimes contain content
        if content_tag is None:
            nos = soup.select_one("noscript")
            if nos and nos.decode_contents().strip():
                content_tag = BeautifulSoup(nos.decode_contents(), "html.parser")

        clean_content = ""

        if content_tag:
            # Remove noisy elements but keep useful paragraphs
            for bad in content_tag.select("script, style, .ads, .chapter-title"):
                bad.decompose()
            # If the element contains paragraphs, keep them; otherwise take inner HTML
            paragraphs = content_tag.find_all("p")
            if paragraphs:
                pieces = []
                for p in paragraphs:
                    text_html = p.decode_contents().strip()
                    if text_html:
                        pieces.append(f"<p>{text_html}</p>")
                clean_content = "\n".join(pieces)
            else:
                clean_content = content_tag.decode_contents().strip()

        # Final fallback: try to assemble from body paragraphs
        if not clean_content:
            body_ps = soup.find_all("p")
            pieces = []
            for p in body_ps:
                t = p.get_text(strip=True)
                if t:
                    pieces.append(f"<p>{t}</p>")
            clean_content = "\n".join(pieces)

        if clean_content:
            try:
                # Use semaphore to limit concurrent file I/O operations
                async with self.file_io_semaphore:
                    async with aiofiles.open(cache_path, "w", encoding="utf-8") as f:
                        await f.write(clean_content)
            except Exception:
                pass  # Silently fail - cache is not critical
            return {"title": chapter_title, "content": clean_content}

        return None

    async def save_raw_pages(self, urls: List[str], out_dir: str) -> List[str]:
        """Fetch raw HTML for each URL and save to `out_dir`.

        Returns list of saved file paths.
        """
        saved_paths: List[str] = []
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        async with AsyncSession(impersonate="chrome120") as session:
            for url in urls:
                try:
                    html = await self._fetch_url(session, url)
                    if not html:
                        print(f"⚠️ No content for {url}")
                        continue

                    fname = hashlib.md5(url.encode()).hexdigest() + ".html"
                    file_path = out_path / fname
                    # Use semaphore to limit concurrent file I/O
                    async with self.file_io_semaphore:
                        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                            await f.write(html)
                    saved_paths.append(str(file_path))
                    print(f"✅ Saved {url} -> {file_path}")
                except Exception as e:
                    print(f"❌ Failed to save {url}: {e}")

        return saved_paths
