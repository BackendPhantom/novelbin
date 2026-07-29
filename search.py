# main.py

from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

from .helpers import _prompt_chapter_indices, _start_background_download
from .utils import build_novelbin_url, clean_filename, is_relevant_result


async def search_mode(scraper, lib, query=None, ainput=None, manager=None):
    if not query:
        if ainput is None:
            query = input("\n🔍 Enter novel name: ").strip()
        else:
            query = (await ainput("\n🔍 Enter novel name: ")).strip()

    # --- Layer 1: Normal search ---
    print("Searching...")
    all_results = await scraper.search_novel(query)

    # Filter to only relevant results
    results = [r for r in all_results if is_relevant_result(query, r["title"])]

    if not results:
        # --- Layer 2: Slug brute-force fallback ---
        if all_results:
            print("⚠️ Search returned results but none matched your query.")
        print("🔎 Trying URL patterns...")
        slug_match = await scraper.try_slug_urls(query)

        if slug_match:
            print(f"✅ Found via URL: {slug_match['title']}")
            results = [slug_match]
        else:
            # --- Layer 3: Manual URL input ---
            print("❌ Could not find the novel automatically.")
            if ainput is None:
                manual_input = input("📎 Enter one novel name or full URL: ").strip()
            else:
                manual_input = (
                    await ainput("📎 Enter one novel name or full URL: ")
                ).strip()
            if not manual_input:
                return

            if "," in manual_input or "\n" in manual_input:
                print(
                    "❌ Multiple novels are not supported yet. Please enter one at a time."
                )
                return

            url = build_novelbin_url(manual_input)
            if not url:
                print("❌ No valid input provided.")
                return

            results = [{"title": "Unknown (from URL)", "url": url}]

    print("\nResults:")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")

    while True:
        try:
            if ainput is None:
                choice_str = input("\nSelect novel number: ")
            else:
                choice_str = await ainput("\nSelect novel number: ")
            choice = int(choice_str)
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
            print("2. 📝 Manual Selection")

            if ainput is None:
                sub_choice = input("Option [1/2]: ").strip()
            else:
                sub_choice = (await ainput("Option [1/2]: ")).strip()

            if sub_choice == "1":
                if diff <= 0:
                    print("✅ Already up to date!")
                    return

                new_indices = list(range(last_cnt, len(all_chapters)))
                await _start_background_download(
                    scraper,
                    lib,
                    manager,
                    novel["url"],
                    title,
                    author,
                    cover,
                    description,
                    all_chapters,
                    new_indices,
                    clean_filename(title),
                    ainput=ainput,
                    add_to_library=True,
                )
                return

            selected_indices = await _prompt_chapter_indices(ainput, len(all_chapters))
            if not selected_indices:
                print("❌ No chapters selected.")
                return

            await _start_background_download(
                scraper,
                lib,
                manager,
                novel["url"],
                title,
                author,
                cover,
                description,
                all_chapters,
                selected_indices,
                clean_filename(title),
                ainput=ainput,
                add_to_library=True,
            )
            return

        if ainput is None:
            fav = input("⭐ Add to Library for tracking? [y/N]: ").strip().lower()
        else:
            fav = (
                (await ainput("⭐ Add to Library for tracking? [y/N]: "))
                .strip()
                .lower()
            )

        selected_indices = await _prompt_chapter_indices(ainput, len(all_chapters))
        if not selected_indices:
            print("❌ No chapters selected.")
            return

        await _start_background_download(
            scraper,
            lib,
            manager,
            novel["url"],
            title,
            author,
            cover,
            description,
            all_chapters,
            selected_indices,
            clean_filename(title),
            ainput=ainput,
            add_to_library=(fav == "y"),
        )
        return
