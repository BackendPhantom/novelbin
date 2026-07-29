import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiofiles
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession, RequestsError

from .auth import NovelArrowAuth
from .config import (
    ADAPTIVE_INTERVAL_STEP,
    ADAPTIVE_STEP_EVERY,
    BASE_URL,
    BURST_COOLDOWN_EVERY,
    BURST_COOLDOWN_MAX,
    BURST_COOLDOWN_MIN,
    CACHE_DIR,
    COOKIE_FILE,
    HEADERS,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    RECOVERY_REQUESTS_AFTER_CHALLENGE,
    REQUEST_MAX_INTERVAL,
    REQUEST_MIN_INTERVAL,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
    TRANSPORT_FAILURES_BEFORE_HTTPX_FALLBACK,
)
from .utils import generate_slug_variants

logger = logging.getLogger(__name__)


class AsyncScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.file_io_semaphore = asyncio.Semaphore(10)

        self.browser_lock = asyncio.Lock()
        self.last_update_time = 0

        self.cache_dir = CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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

        self.auth = NovelArrowAuth(self.cache_dir)
        self.load_cookies()

    def _effective_request_interval(self) -> float:
        step_count = self.total_successful_requests // max(1, ADAPTIVE_STEP_EVERY)
        adaptive = self.min_request_interval + (step_count * ADAPTIVE_INTERVAL_STEP)

        if self.recovery_requests_left > 0:
            adaptive += 0.8

        return min(self.max_request_interval, adaptive)

    async def _wait_for_request_slot(self):
        async with self.request_guard_lock:
            now = time.monotonic()
            elapsed = now - self.last_request_monotonic
            target_interval = self._effective_request_interval()
            delay = max(0.0, target_interval - elapsed)

            delay += random.uniform(0.01, 0.05)

            if delay > 0:
                await asyncio.sleep(delay)

            self.last_request_monotonic = time.monotonic()

    async def _cooldown_if_needed(self):
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
        if "cdn-cgi/challenge-platform" in probe:
            return True
        if "cf-browser-verification" in probe:
            return True
        if "cf_chl" in probe or "cf_chl_" in probe:
            return True
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
            for part in cookie_string.split(";"):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    parsed[k] = v
        return parsed

    def _normalize_solver_payload(
        self, payload: dict
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        if not isinstance(payload, dict):
            return {}, {}
        if "headers" in payload or "cookies" in payload:
            headers = (payload.get("headers") or {}).copy()
            cookies = (payload.get("cookies") or {}).copy()
            if not cookies and payload.get("cookie_string"):
                cookies = self._parse_cookie_string(payload.get("cookie_string", ""))
            return headers, cookies

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
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    saved_payload = json.load(f)

                headers, cookies = self._normalize_solver_payload(saved_payload)
                self.current_headers.update(headers)
                self.current_headers.pop("Cookie", None)
                self.current_cookies.update(cookies)

                self.last_update_time = time.time()
                logger.debug("🍪 Synced cookies from disk.")
            except Exception as e:
                logger.warning(f"⚠️ Failed to load saved cookies: {e}")

    def save_cookies(self):
        try:
            payload = {
                "headers": self.current_headers,
                "cookies": self.current_cookies,
                "updated_at": self.last_update_time,
            }
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
        except Exception as e:
            logger.warning(f"⚠️ Failed to save cookies: {e}")

    def clear_cache_for_url(self, url: str):
        cache_path = Path(self._get_cache_path(url))
        if cache_path.exists():
            cache_path.unlink()

    def _get_cache_path(self, url: str) -> str:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return str(CACHE_DIR / f"{url_hash}.html")

    async def _update_headers_if_needed(self, error_timestamp, prompt_login=False):
        async with self.browser_lock:
            if self.last_update_time > error_timestamp:
                return True

            if prompt_login:
                print("\n🔒 Page requires login. Launching browser to log in...")
            else:
                print("\n🔒 403 Forbidden detected. Engaging auto-solver...")

            self.last_solver_launch_monotonic = time.monotonic()
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

                self.last_update_time = time.time()
                self.recovery_requests_left = RECOVERY_REQUESTS_AFTER_CHALLENGE
                self.save_cookies()
                print("🔄 Headers updated. Retrying request...")
                return True
            return False

    # DEFAULT SET TO TRUE - Every request is now authenticated
    async def _fetch_url(
        self, session: AsyncSession, url: str, require_auth: bool = True
    ) -> Optional[str]:
        if require_auth:
            await self.auth.ensure_authenticated(session)

        for attempt in range(MAX_RETRIES):
            try:
                async with self.semaphore:
                    await self._wait_for_request_slot()

                    if COOKIE_FILE.exists():
                        try:
                            if COOKIE_FILE.stat().st_mtime > self.last_update_time:
                                self.load_cookies()
                        except Exception:
                            pass

                    req_headers = self.current_headers.copy()
                    req_cookies = self.current_cookies.copy()

                    if require_auth:
                        auth_state = self.auth.get_auth_state()

                        # 🔴 THE FIX: Prevent Auth from overwriting Cloudflare's specific User-Agent!
                        auth_headers = auth_state["headers"].copy()
                        if "User-Agent" in req_headers:
                            # Drop the hardcoded User-Agent from auth so the Cloudflare one survives
                            auth_headers.pop("User-Agent", None)

                        req_headers.update(auth_headers)
                        req_cookies.update(auth_state["cookies"])
                    else:
                        req_cookies.pop("auth_access_token", None)
                        req_cookies.pop("auth_refresh_token", None)
                        req_cookies.pop("auth_refresh_persistent", None)

                    response = await session.get(
                        url,
                        headers=req_headers,
                        cookies=req_cookies,
                        timeout=REQUEST_TIMEOUT,
                        impersonate="chrome120",
                    )

                    response_text = response.text

                    response_cookies = self._extract_response_cookies(response)
                    if response_cookies:
                        response_cookies.pop("auth_access_token", None)
                        response_cookies.pop("auth_refresh_token", None)
                        response_cookies.pop("auth_refresh_persistent", None)

                        changed = any(
                            self.current_cookies.get(k) != v
                            for k, v in response_cookies.items()
                        )
                        if changed:
                            self.current_cookies.update(response_cookies)
                            self.last_update_time = time.time()
                            self.save_cookies()

                    if require_auth and (
                        response.status_code == 401
                        or (response.status_code == 403 and self.auth.access_token)
                    ):
                        looks_like_challenge = self._looks_like_cloudflare_challenge(
                            response_text
                        )

                        if not looks_like_challenge:
                            logger.warning(
                                f"⚠️ Server rejected token (HTTP {response.status_code}) for {url}. Forcing reactive re-login..."
                            )

                            login_success = await self.auth.force_relogin(session)
                            if login_success:
                                await asyncio.sleep(1)
                                continue
                            else:
                                logger.error(
                                    "Fatal: Reactive re-authentication failed."
                                )
                                return None

                    if response.status_code == 429:
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )
                        pause = RETRY_DELAY * (attempt + 1)
                        logger.warning(
                            f"Rate limited (429) for {url}. Backing off {pause}s..."
                        )
                        await asyncio.sleep(pause)
                        continue

                    if response.status_code == 404:
                        logger.debug(f"404 Not Found for {url}.")
                        return None

                    looks_like_challenge = self._looks_like_cloudflare_challenge(
                        response_text
                    )
                    if looks_like_challenge or (response.status_code == 403):
                        fail_time = time.time()
                        self.recovery_requests_left = max(
                            self.recovery_requests_left,
                            RECOVERY_REQUESTS_AFTER_CHALLENGE,
                        )
                        logger.info(
                            f"Cloudflare challenge detected for {url}. Waking solver..."
                        )

                        try:
                            success = await self._update_headers_if_needed(
                                fail_time, prompt_login=False
                            )
                            if success:
                                await asyncio.sleep(2)
                                continue
                        except RuntimeError as e:
                            logger.error(f"Solver aborted: {e}")
                            return None

                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue

                        logger.error(
                            f"Cloudflare challenge failed or max retries reached for {url}."
                        )
                        return None

                    response.raise_for_status()
                    self._mark_successful_request()
                    await self._cooldown_if_needed()
                    return response.text

            except (RequestsError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Network error ({url}): {e}. Retrying ({attempt + 1}/{MAX_RETRIES})..."
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                else:
                    logger.error(f"Transport permanently failed for {url}")
                    return None

            except Exception as e:
                logger.exception(f"Unexpected error fetching {url}")
                return None

        logger.error(f"Failed to fetch {url} after {MAX_RETRIES} attempts.")
        return None

    def clean_soup(self, html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["style", "footer", "nav", "aside", "iframe"]):
            tag.decompose()
        for ad in soup.select("div.ads"):
            ad.decompose()
        return soup

    async def search_novel(self, query: str) -> List[Dict]:
        async with AsyncSession(impersonate="chrome120") as session:
            url = f"{BASE_URL}/novels/search?keyword={query.replace(' ', '+')}"
            html = await self._fetch_url(session, url, require_auth=False)
            if not html:
                return []
            soup = self.clean_soup(html)
            results = []
            for item in soup.select(".site-panel.group"):
                link = item.select_one("a[title]")
                if link:
                    href = link["href"]
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    results.append({"title": link["title"], "url": href})
            return results

    async def try_slug_urls(self, query: str) -> Optional[Dict]:
        slugs = generate_slug_variants(query)
        if not slugs:
            return None

        async with AsyncSession(impersonate="chrome120") as session:
            for slug in slugs:
                url = f"{BASE_URL}/novel/{slug}"
                html = await self._fetch_url(session, url, require_auth=False)
                if not html:
                    continue
                soup = self.clean_soup(html)
                results = []
                for item in soup.select(".site-panel.group"):
                    link = item.select_one("a[title]")
                    if link:
                        href = link["href"]
                        if not href.startswith("http"):
                            href = BASE_URL + href
                        results.append({"title": link["title"], "url": href})
                return results
        return None

    async def extract_description(self, html_content: str, soup: BeautifulSoup) -> str:
        match = re.search(
            r'\\?"synopsisParagraphs\\?"\s*:\s*(\[.*?\])\s*(?:,\s*\\?"[a-zA-Z0-9_]+\\?"\s*:|\})',
            html_content,
            re.DOTALL,
        )
        if match:
            try:
                raw_array_str = match.group(1)
                clean_array_str = raw_array_str.replace('\\"', '"').replace("\\'", "'")
                paragraphs: list[str] = json.loads(clean_array_str)
                return "\n".join(paragraphs)
            except json.JSONDecodeError as e:
                print(f"DEBUG: JSON parse failed: {e}")
                pass

        synopsis_divs = soup.select(".classic-novel-detail-synopsis")
        desc_div = max(synopsis_divs, key=lambda d: len(d.get_text()), default=None)
        if desc_div:
            return "\n".join([p.get_text(strip=True) for p in desc_div.find_all("p")])

        return "Description not found."

    async def extract_metadata(self, session: AsyncSession, url: str) -> Tuple:
        novel_slug = url.rstrip("/").split("/")[-1]
        try:
            api_base = f"{BASE_URL}/api-web/novels"
            novel_api_url = f"{api_base}/{novel_slug}/"

            novel_json_str = await self._fetch_url(session, novel_api_url)

            if novel_json_str:
                novel_data = json.loads(novel_json_str)
                novel_info = novel_data.get("item", {}).get("novelInfo")

                if novel_info:
                    title = novel_info.get("novel_name", "Untitled")
                    author = novel_info.get("novel_author")

                    raw_desc = novel_info.get("novel_desc", "")
                    description = ""

                    if raw_desc:
                        desc_soup = BeautifulSoup(raw_desc, "html.parser")
                        description = "\n".join(
                            line.strip()
                            for line in desc_soup.get_text(separator="\n").splitlines()
                            if line.strip()
                        )
                    else:
                        description = "Description not found."

                    cover_url = f"https://images.novelarrow.com/novel/{novel_slug}.jpg"

                    chapters = []
                    page = 1
                    total_pages = 1

                    while page <= total_pages:
                        chapters_api_url = (
                            f"{api_base}/{novel_slug}/chapters/?page={page}&limit=500"
                        )
                        chapters_json_str = await self._fetch_url(
                            session, chapters_api_url
                        )

                        if not chapters_json_str:
                            break

                        chapters_data = json.loads(chapters_json_str)
                        items = chapters_data.get("items", [])

                        for chapter in items:
                            c_id = chapter.get("chapter_id")
                            c_name = chapter.get("chapter_name")
                            if c_id and c_name:
                                c_url = f"{BASE_URL}/chapter/{novel_slug}/{c_id}"
                                chapters.append({"name": c_name, "url": c_url})

                        pagination = chapters_data.get("pagination", {})
                        total_pages = pagination.get("totalPages", 1)
                        page += 1

                    if chapters:
                        return title, author, cover_url, description, chapters

        except Exception as e:
            print(f"DEBUG: API extraction failed or timed out ({e}).")

    async def fetch_chapter_content(
        self, session: AsyncSession, chapter: Dict
    ) -> Optional[Dict[str, str]]:
        cache_path = Path(self._get_cache_path(chapter["url"]))
        async with self.file_io_semaphore:
            if cache_path.exists():
                try:
                    async with aiofiles.open(cache_path, "r", encoding="utf-8") as f:
                        return {
                            "title": chapter.get("name", ""),
                            "content": await f.read(),
                        }
                except Exception:
                    pass

        parts = chapter["url"].rstrip("/").split("/")
        if len(parts) < 2:
            logger.warning(f"Malformed chapter URL: {chapter['url']}")
            return None

        chapter_id = parts[-1]
        novel_slug = parts[-2]

        api_url = (
            f"https://novelarrow.com/api-web/novels/{novel_slug}/chapters/{chapter_id}"
        )
        json_str = await self._fetch_url(session, api_url)

        if json_str:
            try:
                data = json.loads(json_str)
                chapter_info = data.get("item", {}).get("chapterInfo", {})

                raw_content = chapter_info.get("chapter_content", "")

                if raw_content:
                    clean_content = raw_content.strip()
                    chapter_title = chapter_info.get("chapter_name") or chapter.get(
                        "name", ""
                    )

                    try:
                        async with self.file_io_semaphore:
                            async with aiofiles.open(
                                cache_path, "w", encoding="utf-8"
                            ) as f:
                                await f.write(clean_content)
                    except Exception as e:
                        logger.debug(f"Failed to write cache for {chapter_id}: {e}")

                    return {"title": chapter_title, "content": clean_content}

            except json.JSONDecodeError:
                pass

        logger.debug(f"API failed for {chapter_id}. Triggering HTML Fallback...")

        html = await self._fetch_url(session, chapter["url"])
        if not html:
            return None

        soup = self.clean_soup(html)

        article_tag = soup.find("article", attrs={"data-chapter-id": True})

        if not article_tag:
            for sel in [
                "article",
                "#chr-content",
                ".chapter-content",
                "#chapter",
                ".read-content",
                ".content",
            ]:
                article_tag = soup.select_one(sel)
                if article_tag:
                    break

        if not article_tag:
            logger.warning(
                f"HTML fallback failed. No content container found for {chapter_id}"
            )
            return None

        chapter_title = ""
        h2_tag = article_tag.find("h2")
        if h2_tag:
            chapter_title = h2_tag.get_text(strip=True)
            h2_tag.decompose()

        if not chapter_title:
            chapter_title = chapter.get("name", "")

        for bad in article_tag.select("script, style, .ads"):
            bad.decompose()

        paragraphs = article_tag.find_all("p")
        if paragraphs:
            pieces = []
            for p in paragraphs:
                text_html = p.decode_contents().strip()
                if text_html:
                    pieces.append(f"<p>{text_html}</p>")
            clean_content = "\n".join(pieces)
        else:
            clean_content = article_tag.decode_contents().strip()

        if clean_content:
            try:
                async with self.file_io_semaphore:
                    async with aiofiles.open(cache_path, "w", encoding="utf-8") as f:
                        await f.write(clean_content)
            except Exception:
                pass

            return {"title": chapter_title, "content": clean_content}

        return None

    async def save_raw_pages(self, urls: List[str], out_dir: str) -> List[str]:
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
                    async with self.file_io_semaphore:
                        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                            await f.write(html)
                    saved_paths.append(str(file_path))
                    print(f"✅ Saved {url} -> {file_path}")
                except Exception as e:
                    print(f"❌ Failed to save {url}: {e}")

        return saved_paths

    async def _cleanup_cache_async(
        self, max_age_hours: int = 48, max_size_mb: int = 500
    ):
        if not hasattr(self, "cache_dir") or not self.cache_dir.exists():
            return
        logger.info(
            f"🧹 Starting background cache cleanup (TTL: {max_age_hours}h, Limit: {max_size_mb}MB)..."
        )
        try:
            await asyncio.to_thread(
                self._sync_cache_eviction, max_age_hours, max_size_mb
            )
        except Exception as e:
            logger.exception("Failed to complete cache cleanup.")

    def _sync_cache_eviction(self, max_age_hours: int, max_size_mb: int):
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        max_size_bytes = max_size_mb * 1024 * 1024

        purged_by_time = 0
        total_size = 0
        valid_files = []

        for file_path in self.cache_dir.glob("*"):
            if not file_path.is_file():
                continue

            file_stat = file_path.stat()
            file_age = current_time - file_stat.st_mtime

            if file_age > max_age_seconds:
                file_path.unlink(missing_ok=True)
                purged_by_time += 1
            else:
                file_size = file_stat.st_size
                total_size += file_size
                valid_files.append((file_path, file_stat.st_mtime, file_size))

        purged_by_size = 0
        if total_size > max_size_bytes:
            valid_files.sort(key=lambda x: x[1])
            for file_path, _, file_size in valid_files:
                if total_size <= max_size_bytes:
                    break
                file_path.unlink(missing_ok=True)
                total_size -= file_size
                purged_by_size += 1

        if purged_by_time > 0 or purged_by_size > 0:
            logger.info(
                f"✅ Cache cleanup complete. Purged {purged_by_time} old files, "
                f"and {purged_by_size} files to maintain size limits. "
                f"Current cache size: {total_size / (1024*1024):.2f} MB."
            )
