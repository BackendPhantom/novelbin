# main.py

import asyncio
from curl_cffi.requests import AsyncSession

from .helpers import _prompt_chapter_indices, _start_background_download
from .utils import build_novelbin_url, clean_filename, is_relevant_result


async def search_mode(scraper, lib, query=None, ainput=None, manager=None):
    if not query:
        if ainput is None:
            query = input("\n🔍 Enter novel name: ").strip()
        else:
            query = (await ainput("\n🔍 Enter novel name: ")).strip()

    # --- Layer 1: Normal search (Concurrent Buffered UI Pagination) ---
    print("Searching...")

    ITEMS_PER_PAGE = 20
    CONCURRENT_BATCH_SIZE = 5  # Number of pages to fetch simultaneously

    ui_page = 1
    website_page = 1
    website_has_next = True

    global_results = []
    novel = None

    while not novel:
        target_count = ui_page * ITEMS_PER_PAGE

        # 1. Fill the buffer concurrently until we hit the target count
        while len(global_results) < target_count and website_has_next:
            # Create a batch of tasks to fetch multiple pages at once
            tasks = [
                scraper.search_novel(query, page=p)
                for p in range(website_page, website_page + CONCURRENT_BATCH_SIZE)
            ]

            # Execute them concurrently
            batch_results = await asyncio.gather(*tasks)

            # Process the results in order
            for page_results, page_has_next in batch_results:
                # Filter results based on relevance
                filtered = [
                    r for r in page_results if is_relevant_result(query, r["title"])
                ]
                global_results.extend(filtered)
                website_page += 1

                # If this specific page didn't have a 'Next' button, the site is out of pages
                if not page_has_next:
                    website_has_next = False
                    break

        # 2. Slice the global buffer to get the items for the current UI page
        start_idx = (ui_page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        current_display = global_results[start_idx:end_idx]

        # Break out completely if we have no results at all
        if not current_display and not website_has_next and start_idx == 0:
            break

        print(f"\n--- Results (Page {ui_page}) ---")
        if not current_display:
            print("(No more relevant results available)")
        else:
            for i, r in enumerate(current_display, start_idx + 1):
                print(f"{i}. {r['title']}")

        # 3. Determine available navigation actions
        can_go_next = len(global_results) > end_idx or website_has_next
        can_go_prev = ui_page > 1

        # Interactive Prompt Loop
        while True:
            prompt_parts = ["\nSelect novel number"]
            if can_go_next:
                prompt_parts.append("[n]ext page")
            if can_go_prev:
                prompt_parts.append("[p]revious page")
            prompt_parts.append("[q]uit: ")

            prompt = " | ".join(prompt_parts)

            if ainput is None:
                choice_str = input(prompt).strip().lower()
            else:
                choice_str = (await ainput(prompt)).strip().lower()

            if choice_str == "q":
                print("Exiting search.")
                return
            if choice_str == "n" and can_go_next:
                ui_page += 1
                break  # Break inner loop to display next page
            if choice_str == "p" and can_go_prev:
                ui_page -= 1
                break  # Break inner loop to display previous page
            if choice_str.isdigit():
                choice = int(choice_str)
                # Validate selection against the entire accumulated buffer
                if 1 <= choice <= len(global_results):
                    novel = global_results[choice - 1]
                    break
            print("⚠️ Invalid input.")

        if novel:
            break  # Break outer pagination loop

    if not novel:
        # --- Layer 2: Slug brute-force fallback ---
        print("❌ Could not find the novel automatically.")
        print("🔎 Trying URL patterns...")
        slug_match = await scraper.try_slug_urls(query)

        if slug_match:
            print(f"✅ Found via URL: {slug_match['title']}")
            novel = slug_match
        else:
            # --- Layer 3: Manual URL input ---
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

            novel = {"title": "Unknown (from URL)", "url": url}

    # <--- Process the selected novel --->
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters, status = (
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

        fav = "n"
        if not library_entry and status == 0:
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
