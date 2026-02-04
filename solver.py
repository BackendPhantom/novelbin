# novelbin/solver.py
import time

from DrissionPage import ChromiumOptions, ChromiumPage


def get_headers_automatically():
    """
    Launches a browser, allows manual/auto Cloudflare solving,
    and extracts the cookies for the scraper.
    """
    print("\n🤖 Launching browser to bypass Cloudflare...")

    # 1. Configure Browser
    co = ChromiumOptions()
    # set to False so you can see the browser and click the puzzle if needed
    co.headless(False)
    co.set_argument("--no-sandbox")

    # 2. Initialize Browser
    page = ChromiumPage(co)

    try:
        # 3. Go to the site
        page.get("https://novelbin.com")

        print("⏳ Waiting for Cloudflare challenge...")
        print("💡 NOTE: If a puzzle appears, please solve it in the popup window!")

        # 4. Wait until we are actually on the site
        # We check for the specific element '.novel-list' or 'Recent Novels' text
        for _ in range(30):
            if page.ele(".novel-list") or "Novel" in page.title:
                break
            # If still on Cloudflare loop, wait
            time.sleep(1)

        # Extra safety wait to ensure cookies are set
        time.sleep(3)

        # 5. Extract the "Golden Ticket" (Cookies & UA)
        # FIX: Call cookies() first to get the object, THEN call .as_dict()
        cookies = page.cookies().as_dict()

        # User Agent is usually a property string
        user_agent = page.user_agent

        # Convert dictionary to cookie string format just in case
        cookie_string = "; ".join([f"{k}={v}" for k, v in cookies.items()])

        headers = {
            "User-Agent": user_agent,
            "Cookie": cookie_string,
            "Referer": "https://novelbin.com/",
        }
        print(headers)
        print("✅ Cloudflare bypassed! Headers extracted.")
        return headers

    except Exception as e:
        print(f"❌ Auto-solver failed: {e}")
        return None

    finally:
        # Close the browser window
        try:
            page.quit()
        except:
            pass
