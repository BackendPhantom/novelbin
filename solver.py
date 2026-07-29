import logging
import time

from DrissionPage import ChromiumOptions, ChromiumPage

from .config import BASE_URL, BROWSER_PROFILE_DIR

# Initialize the logger for this module
logger = logging.getLogger(__name__)


def get_headers_automatically(prompt_login=False):
    """
    Launches a browser, allows manual/auto Cloudflare solving,
    and extracts the cookies for the scraper.
    If prompt_login is True, waits for the user to log in before extracting cookies.
    """
    logger.info("🤖 Launching browser to bypass Cloudflare...")

    # 1. Configure Browser
    co = ChromiumOptions()
    # set to False so you can see the browser and click the puzzle if needed
    co.headless(False)
    co.set_argument("--no-sandbox")

    # Persist profile between launches so Cloudflare trust survives.
    profile_path = str(BROWSER_PROFILE_DIR)
    try:
        co.set_user_data_path(profile_path)
    except Exception:
        co.set_argument(f"--user-data-dir={profile_path}")

    # 2. Initialize Browser
    page = ChromiumPage(co)

    try:
        # 3. Go to the site
        page.get(BASE_URL)

        logger.info("⏳ Waiting for Cloudflare challenge...")
        logger.info(
            "💡 NOTE: If a puzzle appears, please solve it in the popup window!"
        )
        logger.info(
            "⏱️  This will wait indefinitely until the puzzle is solved or you abort."
        )

        # 4. Wait until we are actually on the site
        max_checks = 600  # ~10 minutes of checks
        checks_done = 0

        while checks_done < max_checks:
            try:
                # Check for success (timeout=0.5 prevents locking the thread for too long)
                if page.ele(".novel-list", timeout=0.5) or "Novel" in page.title:
                    logger.info("✅ Page loaded successfully!")
                    break
            except Exception as e:
                # --- THE KILL SWITCH ---
                # If the user clicks the 'X' on the browser, DrissionPage loses connection.
                # Catching this prevents the immortal zombie thread.
                err_str = str(e).lower()
                if (
                    "disconnected" in err_str
                    or "closed" in err_str
                    or "target" in err_str
                ):
                    raise RuntimeError(
                        "Browser window was closed by the user before completion."
                    ) from e

                # If it's a minor evaluation error, log it to debug and keep trying
                logger.debug(f"Ignored evaluation error during Cloudflare check: {e}")

            checks_done += 1

            # Print progress every 10 seconds
            if checks_done % 10 == 0:
                logger.info(
                    f"   Still waiting ({checks_done} seconds)... (Ctrl+C in terminal to abort)"
                )

            time.sleep(1)
        else:
            # If we hit max_checks, give user option to continue or abort
            logger.warning(
                "⚠️ Still waiting after 10 minutes. The puzzle may be stuck."
            )

            # Note: input() blocks the thread. If deploying headless later, remove this.
            print(
                "   Press Enter to continue waiting, or type 'abort' to cancel: ",
                end="",
            )
            response = input().strip().lower()

            if response == "abort":
                raise RuntimeError("User manually aborted Cloudflare puzzle solving.")

            # Reset counter and keep waiting
            checks_done = 0
            while True:
                try:
                    if page.ele(".novel-list", timeout=0.5) or "Novel" in page.title:
                        logger.info("✅ Page loaded successfully!")
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

                checks_done += 1
                if checks_done % 10 == 0:
                    logger.info(f"   Still waiting ({checks_done} more seconds)...")
                time.sleep(1)

        # 5. If login is needed, wait for the user to log in
        if prompt_login:
            logger.info("🔑 Please log in to novelbin in the browser window.")
            input("   Press Enter here once you've logged in...")
            # Give time for cookies to settle after login
            time.sleep(3)
        else:
            # Extra safety wait to ensure cookies are set
            time.sleep(3)

        # 6. Extract the "Golden Ticket" (Cookies & UA)
        cookies = page.cookies().as_dict()
        user_agent = page.user_agent
        cookie_string = "; ".join([f"{k}={v}" for k, v in cookies.items()])

        headers = {
            "User-Agent": user_agent,
            "Referer": f"{BASE_URL}/",
        }

        payload = {
            "headers": headers,
            "cookies": cookies,
            "cookie_string": cookie_string,
        }

        logger.debug(f"Headers extracted: {headers}")
        logger.info("✅ Cloudflare bypassed! Headers extracted.")
        return payload

    except RuntimeError as e:
        # Expected manual aborts / kill switches
        logger.error(f"❌ Auto-solver aborted: {e}")
        raise e  # Must re-raise so the calling thread knows it failed

    except Exception as e:
        # Unexpected crashes
        logger.exception("❌ Auto-solver encountered a critical failure.")
        raise RuntimeError("Critical solver failure") from e

    finally:
        # Guarantee the browser window is destroyed when the thread exits
        try:
            page.quit()
        except:
            pass
