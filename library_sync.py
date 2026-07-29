import re
from pathlib import Path
from curl_cffi.requests import AsyncSession
from .config import BASE_URL
from .epub_builder import update_epub_metadata_async

async def refresh_cover_mode(scraper, lib, name=None, path=None, url=None):
    print("🔄 Preparing to refresh EPUB metadata and cover...")
    
    # 1. Resolve the URL for secure cover fetching
    novel_url = url
    if not novel_url:
        base_name = name if name else Path(path).stem
        slug = re.sub(r'[^a-z0-9]+', '-', base_name.lower()).strip('-')
        novel_url = f"{BASE_URL.rstrip('/')}/novel/{slug}"

    # 2. Securely fetch metadata and cover image bypassing Cloudflare
    async with AsyncSession(impersonate="chrome") as session:
        meta = await scraper.extract_metadata(session, novel_url)
        
        if meta:
            title, author, cover, description, chapters = meta
            
            cover_bytes = None
            if cover:
                print(f"🖼️ Fetching secure cover image from {cover}...")
                try:
                    resp = await session.get(
                        cover,
                        headers=scraper.current_headers,
                        cookies=scraper.current_cookies,
                        timeout=15,
                        impersonate="chrome120",
                    )
                    if getattr(resp, "status_code", None) == 200:
                        cover_bytes = getattr(resp, "content", None)
                        print("✅ Cover image securely downloaded.")
                    else:
                        print(f"⚠️ Cover download failed: HTTP {getattr(resp, 'status_code', 'unknown')}")
                except Exception as exc:
                    print(f"⚠️ Could not download cover: {exc}")
                    
            # 3. Trigger the updater with secure bytes
            result = await update_epub_metadata_async(
                name=name,
                path=path,
                url=url, # Passes original inputs so the builder finds the file locally
                new_title=title,
                new_author=author,
                new_description=description,
                new_cover_url=cover,
                new_cover_bytes=cover_bytes
            )
        else:
            # Fallback to standard updater if scraper fails
            print(f"⚠️ Scraper could not extract data from {novel_url}. Attempting fallback rebuild...")
            result = await update_epub_metadata_async(name=name, path=path, url=url)
            
    # Check if the result is an error message or a successful file path string
    if result and not result.startswith("Error") and not result.startswith("Failed"):
        print(f"✅ Refresh complete: {result}")
    else:
        print(f"❌ {result}")