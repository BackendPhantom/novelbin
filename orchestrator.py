from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

from .downloader import download_workflow
from .helpers import (
    _batch_output_filename,
    _prompt_chapter_indices,
    _start_background_download,
)
from .task_manager import ScrapeTaskManager
from .utils import clean_filename

import asyncio


async def download_single_novel(scraper, lib, url: str, ainput=None, manager=None):
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters, status = (
            await scraper.extract_metadata(session, url)
        )

        print(f"📖 {title} by {author} ({len(all_chapters)} chapters)")

        library_entry = lib.get_all().get(url)

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
                    return None

                new_indices = list(range(last_cnt, len(all_chapters)))
                await _start_background_download(
                    scraper,
                    lib,
                    manager,
                    url,
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
                return None

            selected_indices = await _prompt_chapter_indices(ainput, len(all_chapters))
            if not selected_indices:
                print("❌ No chapters selected.")
                return None

            await _start_background_download(
                scraper,
                lib,
                manager,
                url,
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
            return None

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
            return None

        await _start_background_download(
            scraper,
            lib,
            manager,
            url,
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
        return None


async def scrape_direct_url_job(scraper, lib, url: str, job, task_manager, ainput=None):
    task_manager.set_status(job, "scraping", "Fetching metadata")
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters, status = (
            await scraper.extract_metadata(session, url)
        )

        job.title = title
        task_manager.set_status(
            job, "processing", f"Downloading {len(all_chapters)} chapters"
        )

        output_filename = _batch_output_filename(title, url)
        result = await download_workflow(
            scraper,
            session,
            title,
            author,
            cover,
            description,
            all_chapters,
            selection_indices=list(range(len(all_chapters))),
            output_filename=output_filename,
            task_manager=task_manager,
            job=job,
            ainput=ainput,
        )

        if not result:
            raise RuntimeError(f"Failed to scrape {title}")

        # Batch downloads don't add to library - user can manually add via 'library' command if needed
        return result


async def scrape_multiple_urls(scraper, lib, urls):
    manager = ScrapeTaskManager(auto_print=False)

    async def runner(job, task_manager):
        return await scrape_direct_url_job(scraper, lib, job.url, job, task_manager)

    jobs, results = await manager.run(urls, runner)

    print("\n=== Batch Summary ===")
    for job, result in zip(jobs, results):
        if job.status == "completed":
            print(f"✅ {job.label} -> {job.output_path}")
        else:
            print(f"❌ {job.label} -> {job.status}: {job.error or result}")

    return jobs, results


async def check_updates(
    scraper, lib_manager, manager=None, interactive: bool = True, ainput=None
):
    """Checks all favorites for new chapters in PARALLEL.
    In interactive mode prompts y/n for each novel one-by-one.
    """
    favorites = lib_manager.get_all()
    if not favorites:
        print("📭 Your library is empty.")
        return

    print(f"\n🔄 Checking updates for {len(favorites)} novels...")

    async with AsyncSession(impersonate="chrome") as session:
        tasks = []
        for url, data in favorites.items():
            tasks.append(check_single_novel(scraper, session, url, data))

        results = await asyncio.gather(*tasks)

        # Collect all updates
        updates_with_data = []
        updates_found_count = 0

        for result in results:
            if not result:
                continue

            updates_found_count += 1
            novel_data, new_indices, current_chapters = result
            title = novel_data["title"]

            if new_indices:
                chapter_start = min(new_indices) + 1
                chapter_end = max(new_indices) + 1
                chapter_range = f"chapters {chapter_start}-{chapter_end}"
                novel_chapter_count = len(new_indices)
            else:
                chapter_range = "no new chapters"
                novel_chapter_count = 0

            updates_with_data.append((result, title, chapter_range, novel_chapter_count))

        if updates_found_count == 0:
            print("\n✅ All your novels are up to date.")
            return

        # Prompt for each novel one by one (no summary list)
        print(f"\n📥 Found {updates_found_count} novel(s) with updates\n")
        selected_downloads = []

        for result, title, chapter_range, chapter_count in updates_with_data:
            novel_data, new_indices, current_chapters = result

            if interactive:
                if ainput is None:
                    choice = (
                        input(f"   📖 {title} {chapter_range}\n   {chapter_count} new chapters.\n   Download? [Y/n]:")
                        .strip()
                        .lower()
                    )
                else:
                    choice_raw = await ainput(
                        f"   📖 {title} {chapter_range}\n   {chapter_count} new chapters.\n   Download? [Y/n]:"
                    )
                    choice = choice_raw.strip().lower()
            else:
                choice = "y"
                print(f"   📖 {title} {chapter_range} -> queued")

            if choice in ["", "y"]:
                selected_downloads.append(
                    {
                        "url": novel_data["url"],
                        "title": title,
                        "author": novel_data["author"],
                        "cover": novel_data["cover"],
                        "description": novel_data["desc"],
                        "all_chapters": current_chapters,
                        "new_indices": new_indices,
                        "current_count": novel_data["current_count"],
                    }
                )

        if not selected_downloads:
            print("\n⏭️  No novels selected for update.")
            return

        # START ALL DOWNLOADS IN BACKGROUND
        print(
            f"\n🚀 Starting background downloads for {len(selected_downloads)} novel(s)..."
        )

        for download_info in selected_downloads:
            await _start_background_download(
                scraper,
                lib_manager,
                manager,
                download_info["url"],
                download_info["title"],
                download_info["author"],
                download_info["cover"],
                download_info["description"],
                download_info["all_chapters"],
                download_info["new_indices"],
                clean_filename(download_info["title"]),
                ainput=None,
                add_to_library=True,
            )

        print("✅ All background downloads queued. Use 'status' or 'watch' to monitor.")


async def check_single_novel(scraper, session, url, data):
    try:
        title, author, cover, description, current_chapters,status = (
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



