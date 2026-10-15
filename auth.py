import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

from curl_cffi.requests import AsyncSession, BrowserType
from dotenv import load_dotenv
from .config import BASE_URL, COOKIE_FILE

load_dotenv()
logger = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def load_credentials() -> Dict[str, str]:
    if PYPROJECT_PATH.exists():
        try:
            with open(PYPROJECT_PATH, "rb") as f:
                data = tomllib.load(f)
                return data.get("tool", {}).get("novelbin", {}).get("auth", {})
        except Exception as e:
            logger.warning(f"Failed to read pyproject.toml: {e}")
    return {}


config_auth = load_credentials()
NOVEL_EMAIL = config_auth.get("novel_email", "") or os.getenv("NOVEL_EMAIL", "")
NOVEL_PASSWORD = config_auth.get("novel_password", "") or os.getenv("NOVEL_PASSWORD", "")


def resolve_impersonate_target(user_agent: str = "") -> str:
    """
    Matches the browser's Chrome major version to a BrowserType supported by
    the installed curl_cffi package so TLS and User-Agent fingerprints align.
    """
    supported = {b.value for b in BrowserType if "chrome" in b.value and "android" not in b.value and "ios" not in b.value}
    if not supported:
        return "chrome"

    match = re.search(r"Chrome/(\d+)", user_agent or "")
    if match:
        major = int(match.group(1))
        exact = f"chrome{major}"
        if exact in supported:
            return exact

        # Pick the closest supported desktop Chrome version
        version_map = {}
        for val in supported:
            m = re.match(r"^chrome(\d+)$", val)
            if m:
                version_map[int(m.group(1))] = val
        if version_map:
            closest_ver = min(version_map.keys(), key=lambda v: abs(v - major))
            return version_map[closest_ver]

    return "chrome"


class NovelArrowAuth:
    def __init__(self, cache_dir: Path):
        self.auth_file = cache_dir / "api_auth.json"
        self.auth_lock = asyncio.Lock()

        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        self.appearance = '{"theme":"dark","fontSize":18}'
        self.session_headers: Dict[str, str] = {}
        self.session_cookies: Dict[str, str] = {}

        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Loads existing tokens, headers, and expiration time from disk."""
        if self.auth_file.exists():
            try:
                with open(self.auth_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.access_token = data.get("access_token", "")
                    self.refresh_token = data.get("refresh_token", "")
                    self.expires_at = float(data.get("expires_at", 0.0) or 0.0)
            except Exception as e:
                logger.error(f"Failed to load auth state from disk: {e}")

        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookie_data = json.load(f)
                    self.session_headers = dict(cookie_data.get("headers") or {})
                    self.session_cookies = dict(cookie_data.get("cookies") or {})
            except Exception as e:
                logger.debug(f"Failed to load shared cookie file: {e}")

    def _save_to_disk(self) -> None:
        """Saves current tokens and expiration time to api_auth.json."""
        try:
            self.auth_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.auth_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "expires_at": self.expires_at,
                    },
                    f,
                    indent=4,
                )
        except Exception as e:
            logger.error(f"Failed to save auth state to disk: {e}")

    def _save_shared_cookies(
        self, headers: Dict[str, str], cookies: Dict[str, str], csrf: str = ""
    ) -> None:
        """Saves the merged headers and authenticated cookies back to cookies.json."""
        self.session_headers = dict(headers)
        self.session_cookies = dict(cookies)
        try:
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "headers": self.session_headers,
                        "cookies": self.session_cookies,
                        "csrf": csrf,
                        "updated_at": time.time(),
                    },
                    f,
                    indent=4,
                )
        except Exception as e:
            logger.warning(f"Failed to update shared cookies file: {e}")

    def get_auth_state(self) -> Dict[str, Any]:
        """Returns the headers and cookies required for authenticated scraper requests."""
        cookies = dict(self.session_cookies)
        cookies["site_appearance_settings"] = self.appearance

        if self.access_token:
            cookies["auth_access_token"] = self.access_token
            cookies["auth_refresh_persistent"] = "1"
        if self.refresh_token:
            cookies["auth_refresh_token"] = self.refresh_token

        headers = {}
        if "User-Agent" in self.session_headers:
            headers["User-Agent"] = self.session_headers["User-Agent"]

        return {
            "headers": headers,
            "cookies": cookies,
        }

    def _is_token_expired(self) -> bool:
        if self.expires_at <= 0:
            return True
        return time.time() >= self.expires_at

    async def ensure_authenticated(self, session: AsyncSession) -> bool:
        """
        Ensures valid authentication before requests.
        Acquires the lock inside _execute_login so parallel tasks do not spam browser launches.
        """
        if self._is_token_expired():
            return await self._execute_login(session)
        return True

    async def force_relogin(self, session: AsyncSession) -> bool:
        """Forces an immediate re-login when the server rejects the current session."""
        logger.warning("Auth session rejected by server. Forcing early re-login...")
        self.access_token = ""
        self.expires_at = 0.0
        return await self._execute_login(session)

    async def _execute_login(self, session: AsyncSession) -> bool:
        """
        1. Opens https://novelping.com/login-email via solver.py and saves headers/cookies to cookies.json.
        2. Reads cookies.json and posts the email/password to https://novelping.com/login-email.
        3. If Cloudflare blocks the POST with 403, falls back to logging in inside the browser directly.
        """
        async with self.auth_lock:
            # Double-check inside the lock so queued tasks return immediately once the first succeeds
            if not self._is_token_expired():
                return True

            if self.expires_at > 0:
                logger.info("  Auth session reached its 3-hour limit. Refreshing proactively...")
            else:
                logger.info("  Authenticating with API...")

            clean_base = (
                BASE_URL.rstrip("/")
                if BASE_URL.startswith("http")
                else f"https://{BASE_URL.rstrip('/')}"
            )
            login_url = f"{clean_base}/login-email"

            email = NOVEL_EMAIL or os.getenv("NOVEL_EMAIL", "")
            password = NOVEL_PASSWORD or os.getenv("NOVEL_PASSWORD", "")

            if not email or not password:
                logger.error("  Missing NOVEL_EMAIL or NOVEL_PASSWORD in .env or pyproject.toml.")
                return False

            from .solver import get_headers_automatically

            # Step 1: Launch browser to /login-email and save request headers + cookies to cookies.json
            try:
                logger.info(f"  Navigating to {login_url} to capture request headers...")
                await asyncio.to_thread(
                    get_headers_automatically,
                    prompt_login=False,
                    is_auth=True,
                    direct_browser_login=False,
                )
            except Exception as e:
                logger.error(f"  Solver failed while capturing login headers: {e}")
                return False

            # Step 2: Load the saved JSON file to use as auth for the POST request
            if not COOKIE_FILE.exists():
                logger.error(f"  Expected auth JSON file not found at {COOKIE_FILE}")
                return False

            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    saved_json = json.load(f)
            except Exception as e:
                logger.error(f"  Failed to read {COOKIE_FILE}: {e}")
                return False

            cf_headers: Dict[str, str] = dict(saved_json.get("headers") or {})
            cf_cookies: Dict[str, str] = dict(saved_json.get("cookies") or {})
            csrf_token: str = str(saved_json.get("csrf") or "")

            impersonate_target = resolve_impersonate_target(cf_headers.get("User-Agent", ""))
            logger.debug(f"Using curl_cffi impersonate profile: {impersonate_target}")

            post_headers = dict(cf_headers)
            post_headers.pop("Cookie", None)
            post_headers["Content-Type"] = "application/json"
            post_headers["Accept"] = "application/json, text/plain, */*"
            post_headers["Origin"] = clean_base
            post_headers["Referer"] = login_url

            payload = {
                "email": email,
                "password": password,
                "_csrf": csrf_token,
                "remember": 1,
                "next": "/",
                "remember_submitted": 1,
            }

            # Step 3: POST username and password using the JSON file headers & cookies
            post_succeeded = False
            try:
                logger.info("  Sending login POST request using saved JSON headers...")
                session.cookies.update(cf_cookies)

                response = await session.post(
                    login_url,
                    json=payload,
                    headers=post_headers,
                    cookies=cf_cookies,
                    timeout=30,
                    impersonate=impersonate_target,
                    allow_redirects=False,
                )

                logger.debug(f"Login POST HTTP Status: {response.status_code}")

                if response.status_code in (200, 302):
                    res_cookies = {}
                    if hasattr(response, "cookies") and hasattr(response.cookies, "get_dict"):
                        res_cookies.update(response.cookies.get_dict())
                    if hasattr(session, "cookies") and hasattr(session.cookies, "get_dict"):
                        res_cookies.update(session.cookies.get_dict())

                    cf_cookies.update(res_cookies)
                    self.access_token = cf_cookies.get("auth_access_token", "")
                    self.refresh_token = cf_cookies.get("auth_refresh_token", "")
                    post_succeeded = True
                else:
                    logger.warning(
                        f"  Direct POST returned HTTP {response.status_code}. "
                        "Falling back to browser-assisted login submission..."
                    )
            except Exception as e:
                logger.warning(f"  Login POST request error ({e}). Falling back to browser login...")

            # Step 4: Fallback — if Cloudflare blocked the curl_cffi POST with 403, submit in the browser
            if not post_succeeded:
                try:
                    browser_data = await asyncio.to_thread(
                        get_headers_automatically,
                        prompt_login=False,
                        is_auth=True,
                        direct_browser_login=True,
                        credentials={"email": email, "password": password},
                    )
                    if not browser_data:
                        return False

                    cf_headers = dict(browser_data.get("headers") or {})
                    cf_cookies = dict(browser_data.get("cookies") or {})
                    csrf_token = str(browser_data.get("csrf") or "")
                    self.access_token = cf_cookies.get("auth_access_token", "")
                    self.refresh_token = cf_cookies.get("auth_refresh_token", "")
                    session.cookies.update(cf_cookies)
                except Exception as e:
                    logger.error(f"  Browser fallback login failed: {e}")
                    return False

            # Step 5: Persist authenticated state to both cookies.json and api_auth.json
            self.expires_at = time.time() + 10800 - 300
            self._save_shared_cookies(cf_headers, cf_cookies, csrf_token)
            self._save_to_disk()
            logger.info("  Authentication complete! Session state saved to JSON.")
            return True