import asyncio
import argparse
from pathlib import Path

# NEW: The Bypass Library
from curl_cffi.requests import AsyncSession

from .config import DEFAULT_OUTPUT_DIR
from .scraper import AsyncScraper
from .epub_builder import create_epub
from .library import LibraryManager
from .utils import parse_chapter_selection


async def download_workflow(
    scraper,
    session,
    title,
    author,
    cover,
    description,
    chapters,
    selection_indices=None,
):
    # 1. Determine Selection
    if selection_indices is None:
        sel_str = input(f"Select chapters (Total: {len(chapters)}) [Enter for ALL]: ")
        indices = parse_chapter_selection(sel_str, len(chapters))
    else:
        indices = selection_indices

    selected_chapters = [chapters[i] for i in indices]

    if not selected_chapters:
        print("❌ No chapters selected.")
        return

    print(f"\n🚀 Downloading {len(selected_chapters)} chapters...")

    total = len(selected_chapters)
    completed_count = 0

    async def fetch_with_progress(ch):
        nonlocal completed_count
        # Pass the session to the scraper
        content = await scraper.fetch_chapter_content(session, ch)
        completed_count += 1

        print(f"\rProgress: {completed_count}/{total} processed", end="", flush=True)

        if content is None:
            return None

        return {"title": ch["name"], "content": content}

    # 2. Run All Downloads (Parallel & Non-Blocking)
    tasks = [fetch_with_progress(ch) for ch in selected_chapters]
    results = await asyncio.gather(*tasks)
    print()  # New line after progress bar

    # 3. Separate Successes from Failures
    downloaded_data = []
    failed_chapters = []

    for i, res in enumerate(results):
        if res is None:
            failed_chapters.append(selected_chapters[i]["name"])
        else:
            downloaded_data.append(res)

    # 4. The "Safety Stop" Logic
    if failed_chapters:
        print(f"\n⚠️ Completed with {len(failed_chapters)} errors.")
        print(
            f"🛑 EPUB creation halted. Missing chapters: {', '.join(failed_chapters[:5])}..."
        )
        print(
            "💡 ACTION: Run the script again. It will auto-skip the successful ones and retry ONLY these failures."
        )
        return

    # 5. Build EPUB
    print("\n📦 Building EPUB...")
    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        create_epub(title, author, cover, description, downloaded_data, out_dir)

        # Auto-Cleanup
        print("🧹 Cleaning up cache...", end=" ")
        for ch in selected_chapters:
            scraper.clear_cache_for_url(ch["url"])
        print("Done!")

    except Exception as e:
        print(f"\n❌ Failed to create EPUB: {e}")


async def check_updates(scraper, lib_manager):
    """Checks all favorites for new chapters."""
    favorites = lib_manager.get_all()
    if not favorites:
        print("📭 Your library is empty.")
        return

    print(f"\n🔄 Checking updates for {len(favorites)} novels...")

    # NEW: Initialize AsyncSession with Chrome impersonation
    async with AsyncSession(impersonate="chrome") as session:
        updates_found = False

        for url, data in favorites.items():
            print(f"Checking: {data['title']}...", end=" ", flush=True)
            try:
                # We scrape metadata to get current chapter count
                title, author, cover, description, current_chapters = (
                    await scraper.extract_metadata(session, url)
                )
                current_count = len(current_chapters)
                last_count = data.get("last_count", 0)

                if current_count > last_count:
                    new_chapters_count = current_count - last_count
                    print(f"✨ FOUND {new_chapters_count} NEW CHAPTERS!")
                    updates_found = True

                    choice = (
                        input(
                            f"   Download new chapters ({last_count+1}-{current_count})? [Y/n]: "
                        )
                        .strip()
                        .lower()
                    )
                    if choice in ["", "y"]:
                        new_indices = list(range(last_count, current_count))

                        await download_workflow(
                            scraper,
                            session,
                            title,
                            author,
                            cover,
                            description,
                            current_chapters,
                            new_indices,
                        )

                        # Update library count
                        lib_manager.add_novel(title, url, current_count)
                else:
                    print("Up to date.")

            except Exception as e:
                print(f"Error: {e}")

        if not updates_found:
            print("\n✅ All your novels are up to date.")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--query", help="Quick search")
    args = parser.parse_args()

    scraper = AsyncScraper()
    lib = LibraryManager()

    # If query provided via CLI, skip menu
    if args.query:
        await search_mode(scraper, lib, args.query)
        return

    while True:
        print("\n=== 📚 NOVEL MANAGER ===")
        print("1. 🔍 Search & Download")
        print("2. 🔄 Check for Updates (Library)")
        print("3. ❌ Exit")

        choice = input("Select option: ").strip()

        if choice == "1":
            await search_mode(scraper, lib)
        elif choice == "2":
            await check_updates(scraper, lib)
        elif choice == "3":
            print("Bye!")
            break
        else:
            print("Invalid option")


async def search_mode(scraper, lib, query=None):
    if not query:
        query = input("\n🔍 Enter novel name: ").strip()

    print("Searching...")
    # Scraper handles session internally for search
    results = await scraper.search_novel(query)

    if not results:
        print("❌ No novels found.")
        return

    print("\nResults:")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")

    while True:
        try:
            choice = int(input("\nSelect novel number: "))
            if 1 <= choice <= len(results):
                novel = results[choice - 1]
                break
        except ValueError:
            pass

    # NEW: Initialize AsyncSession with Chrome impersonation
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters = (
            await scraper.extract_metadata(session, novel["url"])
        )
        print(f"📖 {title} by {author} ({len(all_chapters)} chapters)")

        # Ask to Add to Library
        if novel["url"] not in lib.get_all():
            while True:
                fav = input("⭐ Add to Library for tracking? [y/N]: ").strip().lower()
                if fav in ("y", "n"):
                    break
            
            if fav == "y":
                lib.add_novel(title, novel["url"], len(all_chapters))

        # Proceed to Download
        await download_workflow(
            scraper, session, title, author, cover, description, all_chapters
        )


def start():
    """
    Synchronous entry point for installed modules/CLI.
    Wraps the async main function so it runs properly.
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")


if __name__ == "__main__":
    start()
