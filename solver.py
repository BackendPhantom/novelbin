import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from DrissionPage import ChromiumOptions, ChromiumPage
from .config import BASE_URL, BROWSER_PROFILE_DIR, COOKIE_FILE

logger = logging.getLogger(__name__)

# Directory where a version-pinned Chrome for Testing binary can be stored
PINNED_BROWSER_ROOT = Path.home() / ".local" / "share" / "novelbin-browser"


def _find_pinned_chrome_binary() -> Optional[Path]:
    """
    Locates a version-pinned Chrome/Chromium executable so curl_cffi and
    the browser share the exact same TLS and User-Agent fingerprint.
    """
    env_path = os.getenv("NOVELBIN_CHROME_PATH", "").strip()
    if env_path:
        custom_bin = Path(env_path).expanduser()
        if custom_bin.is_file():
            return custom_bin

    if PINNED_BROWSER_ROOT.exists():
        for candidate in PINNED_BROWSER_ROOT.rglob("chrome"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    return None


def _build_consistent_headers(
    captured_headers: Dict[str, str], user_agent: str, base_url: str
) -> Dict[str, str]:
    """
    Normalizes captured browser request headers so curl_cffi sends a
    consistent set of Client Hints and Origin/Referer headers.
    """
    match = re.search(r"Chrome/(\d+)", user_agent)
    major_ver = match.group(1) if match else "150"
    is_mobile = "?1" if "Mobile" in user_agent else "?0"
    if "Android" in user_agent:
        platform = '"Android"'
    elif "Windows" in user_agent:
        platform = '"Windows"'
    elif "Mac OS" in user_agent:
        platform = '"macOS"'
    else:
        platform = '"Linux"'

    clean_base = base_url.rstrip("/")

    # Start with clean defaults matching the browser
    headers: Dict[str, str] = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": f'"Chromium";v="{major_ver}", "Google Chrome";v="{major_ver}", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": is_mobile,
        "sec-ch-ua-platform": platform,
        "Origin": clean_base,
        "Referer": f"{clean_base}/login-email",
    }

    # Merge any real headers captured from the browser navigation packet
    allowed_captured = {
        "user-agent": "User-Agent",
        "accept-language": "Accept-Language",
        "sec-ch-ua": "sec-ch-ua",
        "sec-ch-ua-mobile": "sec-ch-ua-mobile",
        "sec-ch-ua-platform": "sec-ch-ua-platform",
    }
    for k, v in (captured_headers or {}).items():
        norm_key = k.lower()
        if norm_key in allowed_captured and v:
            headers[allowed_captured[norm_key]] = v

    return headers


def _extract_csrf_token(page: ChromiumPage) -> str:
    """Extracts the _csrf token from the login page DOM or HTML source."""
    try:
        csrf_ele = page.ele('xpath://input[@name="_csrf"]', timeout=1)
        if csrf_ele and csrf_ele.attr("value"):
            return str(csrf_ele.attr("value")).strip()
    except Exception:
        pass

    try:
        csrf_meta = page.ele('xpath://meta[@name="csrf-token"]', timeout=1)
        if csrf_meta and csrf_meta.attr("content"):
            return str(csrf_meta.attr("content")).strip()
    except Exception:
        pass

    try:
        html = page.html or ""
        match = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', html)
        if not match:
            match = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']_csrf["\']', html)
        if match:
            return match.group(1).strip()
    except Exception:
        pass

    return ""


def get_headers_automatically(
    prompt_login: bool = False,
    is_auth: bool = False,
    direct_browser_login: bool = False,
    credentials: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Launches a browser, solves Cloudflare challenges, and extracts headers/cookies.
    When is_auth=True, navigates to https://novelping.com/login-email, captures all
    request headers + cookies + CSRF token, and saves them to COOKIE_FILE (cookies.json).
    """
    logger.info("  Launching browser to bypass Cloudflare...")

    co = ChromiumOptions()
    co.headless(False)
    co.set_argument("--no-sandbox")
    # Use a dedicated port so the scraper browser never collides with a running Chrome instance
    co.set_local_port(9223)

    pinned_bin = _find_pinned_chrome_binary()
    if pinned_bin:
        logger.info(f"  Using version-pinned browser binary: {pinned_bin}")
        co.set_browser_path(str(pinned_bin))

    profile_path = str(BROWSER_PROFILE_DIR)
    try:
        co.set_user_data_path(profile_path)
    except Exception:
        co.set_argument(f"--user-data-dir={profile_path}")

    page = ChromiumPage(co)
    captured_req_headers: Dict[str, str] = {}

    try:
        clean_base = (
            BASE_URL.rstrip("/")
            if BASE_URL.startswith("http")
            else f"https://{BASE_URL.rstrip('/')}"
        )
        target_url = f"{clean_base}/login-email" if is_auth else clean_base

        # Start listening to network packets to capture the exact request headers sent by the browser
        try:
            page.listen.start("login-email" if is_auth else clean_base)
        except Exception as e:
            logger.debug(f"Could not start network listener: {e}")

        page.get(target_url)

        logger.info("  Waiting for Cloudflare challenge...")
        logger.info(
            "  NOTE: If a puzzle appears, please solve it in the popup window!"
        )

        max_checks = 600  # ~10 minutes
        checks_done = 0

        while checks_done < max_checks:
            try:
                if is_auth:
                    if (
                        page.ele("@name=email", timeout=0.5)
                        or page.ele("@name=_csrf", timeout=0.5)
                        or "login" in (page.title or "").lower()
                    ):
                        logger.info("  Login page loaded successfully!")
                        break
                else:
                    if page.ele(".novel-list", timeout=0.5) or "Novel" in (page.title or ""):
                        logger.info("  Page loaded successfully!")
                        break
            except Exception as e:
                err_str = str(e).lower()
                if (
                    "disconnected" in err_str
                    or "closed" in err_str
                    or "target" in err_str
                ):
                    raise RuntimeError(
                        "Browser window was closed by the user before completion."
                    ) from e
                logger.debug(f"Ignored evaluation error during Cloudflare check: {e}")

            checks_done += 1
            if checks_done % 10 == 0:
                logger.info(
                    f"   Still waiting ({checks_done} seconds)... (Ctrl+C in terminal to abort)"
                )
            time.sleep(1)
        else:
            raise RuntimeError("Timed out waiting for Cloudflare challenge to clear.")

        # Capture the actual request headers from the network listener if available
        try:
            packet = page.listen.wait(timeout=1.5)
            if packet and hasattr(packet, "request") and packet.request.headers:
                captured_req_headers = dict(packet.request.headers)
        except Exception:
            pass
        finally:
            try:
                page.listen.stop()
            except Exception:
                pass

        csrf_token = ""
        if is_auth:
            csrf_token = _extract_csrf_token(page)
            logger.debug(f"Extracted CSRF token from /login-email: {bool(csrf_token)}")

        # Optional fallback: submit login form directly inside the browser if requested
        if is_auth and direct_browser_login and credentials:
            email = credentials.get("email", "")
            password = credentials.get("password", "")
            if email and password:
                logger.info("  Submitting credentials directly inside the browser...")
                try:
                    email_input = page.ele("@name=email", timeout=2)
                    pass_input = page.ele("@name=password", timeout=2)
                    if email_input and pass_input:
                        email_input.input(email, clear=True)
                        pass_input.input(password, clear=True)
                        submit_btn = (
                            page.ele('xpath://button[@type="submit"]', timeout=1)
                            or page.ele("@type=submit", timeout=1)
                        )
                        if submit_btn:
                            submit_btn.click()
                            page.wait.url_change("login-email", exclude=True, timeout=15)
                            time.sleep(2)
                except Exception as e:
                    logger.warning(f"  Browser form submission encountered an issue: {e}")

        if prompt_login:
            logger.info("  Please log in to the site in the browser window.")
            input("   Press Enter here once you've logged in...")
            time.sleep(3)
        else:
            time.sleep(2)

        cookies = page.cookies().as_dict()
        user_agent = page.user_agent
        headers = _build_consistent_headers(captured_req_headers, user_agent, clean_base)
        cookie_string = "; ".join([f"{k}={v}" for k, v in cookies.items()])

        payload: Dict[str, Any] = {
            "headers": headers,
            "cookies": cookies,
            "cookie_string": cookie_string,
            "csrf": csrf_token,
            "updated_at": time.time(),
        }

        # Always persist extracted headers and cookies to the shared JSON file
        try:
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
            logger.info(f"  Saved extracted headers and cookies to {COOKIE_FILE}")
        except Exception as e:
            logger.warning(f"  Failed to write {COOKIE_FILE}: {e}")

        logger.info("  Cloudflare bypassed! Headers extracted.")
        return payload

    except RuntimeError as e:
        logger.error(f"  Auto-solver aborted: {e}")
        raise e
    except Exception as e:
        logger.exception("  Auto-solver encountered a critical failure.")
        raise RuntimeError("Critical solver failure") from e
    finally:
        try:
            page.quit()
        except Exception:
            pass