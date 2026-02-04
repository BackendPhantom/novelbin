# main.py
import argparse
import asyncio
from pathlib import Path

from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

from .config import DEFAULT_OUTPUT_DIR
from .epub_builder import create_epub
from .library import LibraryManager
from .scraper import AsyncScraper
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
    """
    Downloads chapters and builds EPUB.
    Returns: True if successful, False if failed.
    """
    if selection_indices is None:
        sel_str = input(f"Select chapters (Total: {len(chapters)}) [Enter for ALL]: ")
        indices = parse_chapter_selection(sel_str, len(chapters))
    else:
        indices = selection_indices

    selected_chapters = [chapters[i] for i in indices]

    if not selected_chapters:
        print("❌ No chapters selected.")
        return False

    print(f"\n🚀 Downloading {len(selected_chapters)} chapters...")

    total = len(selected_chapters)
    completed_count = 0

    async def fetch_with_progress(ch):
        nonlocal completed_count
        content = await scraper.fetch_chapter_content(session, ch)
        completed_count += 1
        print(f"\rProgress: {completed_count}/{total} processed", end="", flush=True)

        if content is None:
            return None
        return {"title": ch["name"], "content": content}

    tasks = [fetch_with_progress(ch) for ch in selected_chapters]
    results = await asyncio.gather(*tasks)
    print()

    downloaded_data = []
    failed_chapters = []

    for i, res in enumerate(results):
        if res is None:
            failed_chapters.append(selected_chapters[i]["name"])
        else:
            downloaded_data.append(res)

    if failed_chapters:
        print(f"\n⚠️ Completed with {len(failed_chapters)} errors.")
        print(
            f"🛑 EPUB creation halted. Missing chapters: {', '.join(failed_chapters[:5])}..."
        )
        return False

    print("\n📦 Building EPUB...")
    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        create_epub(
            title,
            author,
            cover,
            description,
            downloaded_data,
            out_dir,
            headers=scraper.current_headers,
        )
        print("🧹 Cleaning up cache...", end=" ")
        for ch in selected_chapters:
            scraper.clear_cache_for_url(ch["url"])
        print("Done!")
        return True
    except Exception as e:
        print(f"\n❌ Failed to create EPUB: {e}")
        return False


async def check_updates(scraper, lib_manager):
    """Checks all favorites for new chapters in PARALLEL."""
    favorites = lib_manager.get_all()
    if not favorites:
        print("📭 Your library is empty.")
        return

    print(f"\n🔄 Checking updates for {len(favorites)} novels...")

    # <--- CHANGED: Use AsyncSession with impersonate="chrome"
    async with AsyncSession(impersonate="chrome") as session:
        tasks = []
        for url, data in favorites.items():
            tasks.append(check_single_novel(scraper, session, url, data))

        results = await asyncio.gather(*tasks)

        updates_found_count = 0
        for result in results:
            if not result:
                continue

            updates_found_count += 1
            novel_data, new_indices, current_chapters = result

            # Unpack
            title = novel_data["title"]
            print(
                f"\n✨ {title}: Found {novel_data['current_count'] - novel_data['last_count']} new chapters!"
            )

            choice = input(f"   Download? [Y/n]: ").strip().lower()

            if choice in ["", "y"]:
                success = await download_workflow(
                    scraper,
                    session,
                    title,
                    novel_data["author"],
                    novel_data["cover"],
                    novel_data["desc"],
                    current_chapters,
                    new_indices,
                )
                if success:
                    lib_manager.add_novel(
                        title, novel_data["url"], novel_data["current_count"]
                    )

        if updates_found_count == 0:
            print("\n✅ All your novels are up to date.")


async def check_single_novel(scraper, session, url, data):
    try:
        title, author, cover, description, current_chapters = (
            await scraper.extract_metadata(session, url)
        )
        current_count = len(current_chapters)
        last_count = data.get("last_count", 0)

        if current_count > last_count:
            return (
                {
                    "title": title,
                    "author": author,
                    "cover": cover,
                    "desc": description,
                    "last_count": last_count,
                    "current_count": current_count,
                    "url": url,
                },
                list(range(last_count, current_count)),
                current_chapters,
            )
    except Exception as e:
        print(f"⚠️ Error checking {data['title']}: {e}")
    return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--query", help="Quick search")
    args = parser.parse_args()

    scraper = AsyncScraper()
    lib = LibraryManager()

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

    # <--- CHANGED: Use AsyncSession with impersonate="chrome"
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters = (
            await scraper.extract_metadata(session, novel["url"])
        )
        print(f"📖 {title} by {author} ({len(all_chapters)} chapters)")

        library_entry = lib.get_all().get(novel["url"])

        if library_entry:
            last_cnt = library_entry.get("last_count", 0)
            diff = len(all_chapters) - last_cnt
            print(f"\n📂 This novel is in your library (Last read: {last_cnt})")
            print(
                f"1. ⚡ Quick Update (Download chapters {last_cnt+1}-{len(all_chapters)})"
            )
            print(f"2. 📝 Manual Selection")

            sub_choice = input("Option [1/2]: ").strip()

            if sub_choice == "1":
                if diff <= 0:
                    print("✅ Already up to date!")
                    return

                new_indices = list(range(last_cnt, len(all_chapters)))
                success = await download_workflow(
                    scraper,
                    session,
                    title,
                    author,
                    cover,
                    description,
                    all_chapters,
                    new_indices,
                )
                if success:
                    lib.add_novel(title, novel["url"], len(all_chapters))
                return

        # Standard Workflow
        if not library_entry:
            fav = input("⭐ Add to Library for tracking? [y/N]: ").strip().lower()

        success = await download_workflow(
            scraper, session, title, author, cover, description, all_chapters
        )

        if success and not library_entry and fav == "y":
            lib.add_novel(title, novel["url"], len(all_chapters))


def start():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")


if __name__ == "__main__":
    start()
