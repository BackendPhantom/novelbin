import asyncio
import json
import logging
import os
import time
from pathlib import Path

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

import sys

# Use the built-in TOML reader
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# 1. Find the pyproject.toml file dynamically
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def load_credentials():
    if PYPROJECT_PATH.exists():
        with open(PYPROJECT_PATH, "rb") as f:
            data = tomllib.load(f)
            return data.get("tool", {}).get("novelbin", {}).get("auth", {})
    else:
        print(f"⚠️ Warning: pyproject.toml not found at {PYPROJECT_PATH}")
        return {}


# 3. Load variables safely
config_auth = load_credentials()
NOVEL_EMAIL = config_auth.get("novel_email", "")
NOVEL_PASSWORD = config_auth.get("novel_password", "")

# IMPORT SHARED COOKIE FILE CONFIG
from .config import COOKIE_FILE


class NovelArrowAuth:
    def __init__(self, cache_dir: Path):
        self.auth_file = cache_dir / "api_auth.json"
        self.auth_lock = asyncio.Lock()

        # State
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0  # Timestamp of when the token dies
        self.appearance = '{"theme":"dark","fontSize":18}'

        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

        self._load_from_disk()

    def _load_from_disk(self):
        """Loads existing tokens and their expiration time from disk."""
        if self.auth_file.exists():
            try:
                with open(self.auth_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.access_token = data.get("access_token", "")
                    self.refresh_token = data.get("refresh_token", "")
                    self.expires_at = data.get("expires_at", 0.0)
            except Exception as e:
                logger.error(f"Failed to load auth state from disk: {e}")

    def _save_to_disk(self):
        """Saves current tokens and expiration time to disk."""
        try:
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

    def get_auth_state(self) -> dict:
        """Returns the headers and cookies required for requests."""
        return {
            "headers": {
                "User-Agent": self.user_agent,
            },
            "cookies": {
                "auth_refresh_token": self.refresh_token,
                "auth_access_token": self.access_token,
                "auth_refresh_persistent": "1",
                "site_appearance_settings": self.appearance,
            },
        }

    def _is_token_expired(self) -> bool:
        """Returns True if the token is missing or has passed its expiration time."""
        if not self.access_token:
            return True
        return time.time() >= self.expires_at

    async def ensure_authenticated(self, session: AsyncSession) -> bool:
        """
        Called before EVERY request.
        Logs in only if we don't have a token, or if the 3 hours are up.
        """
        if self._is_token_expired():
            if self.access_token:
                logger.info(
                    "⏱️ Auth token reached its 3-hour limit. Refreshing proactively..."
                )
            return await self._execute_login(session)
        return True

    async def force_relogin(self, session: AsyncSession) -> bool:
        """Called if the server unexpectedly returns a 401 Unauthorized before the 3 hours are up."""
        logger.warning("Auth token rejected by server (401). Forcing early re-login...")
        self.access_token = ""
        self.expires_at = 0.0
        return await self._execute_login(session)

    async def _execute_login(self, session: AsyncSession) -> bool:
        """The core login logic wrapped in a lock with internal network retries."""
        async with self.auth_lock:
            if not self._is_token_expired():
                return True

            logger.info("🔑 Authenticating with API...")

            login_url = "https://novelarrow.com/api/login"
            payload = {
                "email": os.getenv("NOVEL_EMAIL", ""),
                "password": os.getenv("NOVEL_PASSWORD", ""),
            }

            login_headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            }
            login_cookies = {}

            # --- SYNCHRONIZE WITH SCRAPER CLOUDFLARE CLEARANCE ---
            if COOKIE_FILE.exists():
                try:
                    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                        scraper_data = json.load(f)
                        cf_headers = scraper_data.get("headers", {})
                        cf_cookies = scraper_data.get("cookies", {})

                        login_headers.update(cf_headers)
                        login_headers.pop(
                            "Cookie", None
                        )  # Clean drop for dictionary management
                        login_cookies.update(cf_cookies)
                        logger.debug(
                            "Loaded Cloudflare clearance cookies from shared cache."
                        )
                except Exception as e:
                    logger.warning(f"Failed to load shared cookies: {e}")

            max_login_retries = 3

            for attempt in range(max_login_retries):
                try:
                    if attempt > 0:
                        logger.info(
                            f"🔄 Retrying login (Attempt {attempt + 1}/{max_login_retries})..."
                        )

                    response = await session.post(
                        login_url,
                        json=payload,
                        headers=login_headers,
                        cookies=login_cookies,
                        timeout=30,
                        impersonate="chrome120",
                    )

                    if response.status_code == 200:
                        res_cookies = response.cookies.get_dict()

                        self.access_token = res_cookies.get("auth_access_token", "")
                        self.refresh_token = res_cookies.get("auth_refresh_token", "")

                        if not self.access_token:
                            logger.error(
                                "Login returned 200, but auth_access_token was missing."
                            )
                            return False

                        self.expires_at = time.time() + 10800 - 300
                        logger.info(
                            "✅ Login successful! Token valid for the next 3 hours."
                        )
                        self._save_to_disk()
                        return True

                    elif response.status_code in [403, 503]:
                        logger.warning(
                            f"Cloudflare blocked login endpoint (HTTP {response.status_code})."
                        )

                        # --- INTEGRATED SOLVER LOGIC ---
                        logger.info(
                            "🚀 Launching solver to bypass Cloudflare for login..."
                        )
                        try:
                            from .solver import get_headers_automatically

                            cf_data = await asyncio.to_thread(
                                get_headers_automatically, prompt_login=False
                            )

                            if cf_data:
                                cf_headers = cf_data.get("headers", {})
                                cf_cookies = cf_data.get("cookies", {})

                                login_headers.update(cf_headers)
                                login_headers.pop("Cookie", None)
                                login_cookies.update(cf_cookies)

                                # Save the new clearance back to the shared file for scraper.py
                                try:
                                    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                                        json.dump(
                                            {
                                                "headers": cf_headers,
                                                "cookies": cf_cookies,
                                                "updated_at": time.time(),
                                            },
                                            f,
                                            indent=4,
                                        )
                                    logger.debug(
                                        "Saved fresh Cloudflare clearance to shared cache."
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"Failed to save shared cookies: {e}"
                                    )

                                logger.info(
                                    "✅ Obtained Cloudflare clearance. Retrying login immediately..."
                                )
                                continue

                        except Exception as e:
                            logger.error(f"Solver failed during login bypass: {e}")

                        if attempt < max_login_retries - 1:
                            await asyncio.sleep(3)
                            continue
                        return False

                    else:
                        logger.error(
                            f"❌ Login failed! HTTP {response.status_code}: {response.text}"
                        )
                        return False

                except Exception as e:
                    logger.warning(f"⚠️ Login network error: {e}")

                    if attempt < max_login_retries - 1:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    else:
                        logger.exception(
                            "❌ Max login retries reached. API is completely unreachable."
                        )
                        return False

            return False
