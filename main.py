# main.py
import argparse
import asyncio
import hashlib
import re
from pathlib import Path

from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

from .config import DEFAULT_OUTPUT_DIR
from .utils import (
    clean_filename,
    parse_chapter_selection,
    is_relevant_result,
    build_novelbin_url,
    build_novelbin_urls,
)
from .task_manager import ScrapeTaskManager


async def download_workflow(
    scraper,
    session,
    title,
    author,
    cover,
    description,
    chapters,
    selection_indices=None,
    output_filename=None,
    task_manager=None,
    job=None,
    ainput=None,
    suppress_progress: bool = False,
):
    """
    Downloads chapters and builds EPUB.
    Returns: True if successful, False if failed.
    """
    if selection_indices is None:
        if task_manager is not None and job is not None:
            indices = list(range(len(chapters)))
        else:
            if ainput is None:
                # Fallback to blocking input if ainput not provided
                sel_str = input(
                    f"Select chapters (Total: {len(chapters)}) [Enter for ALL]: "
                )
            else:
                sel_str = await ainput(
                    f"Select chapters (Total: {len(chapters)}) [Enter for ALL]: "
                )
            indices = parse_chapter_selection(sel_str, len(chapters))
    else:
        indices = selection_indices

    selected_chapters = [chapters[i] for i in indices]

    if not selected_chapters:
        print("❌ No chapters selected.")
        return False

    if task_manager is None or job is None:
        print(f"\n🚀 Downloading {len(selected_chapters)} chapters...")

    total = len(selected_chapters)
    completed_count = 0
    last_update = 0
    update_interval = 0.5  # seconds

    async def fetch_with_progress(ch):
        nonlocal completed_count, last_update
        result = await scraper.fetch_chapter_content(session, ch)
        completed_count += 1

        # When running under a task manager, avoid inline \r progress prints
        # which clobber interactive prompts. Instead emit status updates
        # via the task manager so output stays line-oriented.
        import time

        now = time.time()
        if task_manager is not None and job is not None:
            if now - last_update >= update_interval:
                task_manager.set_status(
                    job, "processing", f"Downloaded {completed_count}/{total} chapters"
                )
                last_update = now
        else:
            # In background mode, avoid progress output that interferes with prompts.
            if not suppress_progress and now - last_update >= update_interval:
                print(
                    f"\rProgress: {completed_count}/{total} processed",
                    end="",
                    flush=True,
                )
                last_update = now

        if result is None:
            return None
        # Result now contains both title and content extracted from the chapter HTML
        chapter_title = result.get("title", "").strip()
        if not chapter_title:
            # Fallback to chapter metadata name or default
            chapter_title = (ch.get("name") or ch.get("title") or "").strip()
        if not chapter_title:
            chapter_title = f"Chapter {completed_count}"
        return {"title": chapter_title, "content": result["content"]}

    tasks = [fetch_with_progress(ch) for ch in selected_chapters]
    results = await asyncio.gather(*tasks)

    # Map chapter index to downloaded content (preserves order)
    chapter_downloads = {}  # {index: content_dict or None}
    failed_indices = []  # Track indices of failed chapters for retry

    for i, res in enumerate(results):
        chapter_downloads[i] = res
        if res is None:
            failed_indices.append(i)

    # RETRY LOGIC: If there are failures, retry them after initial pass
    if failed_indices:
        failed_names = [selected_chapters[i]["name"] for i in failed_indices]
        print(
            f"\n⚠️ {len(failed_indices)} chapter(s) failed on first attempt: {', '.join(failed_names[:3])}"
        )
        if len(failed_names) > 3:
            print(f"   ... and {len(failed_names) - 3} more")
        print(f"ℹ️ Continuing with remaining chapters...")

        if task_manager is not None and job is not None:
            task_manager.set_status(
                job,
                "processing",
                f"Downloaded {len(results) - len(failed_indices)}/{total} chapters (retrying {len(failed_indices)} failed)",
            )

    # After all chapters have been attempted, now retry the failed ones
    if failed_indices:
        print(f"\n🔄 Retrying {len(failed_indices)} failed chapter(s)...")

        # Prepare retry tasks for failed chapters only
        retry_tasks = []
        for idx in failed_indices:
            ch = selected_chapters[idx]
            retry_tasks.append((idx, fetch_with_progress(ch)))

        # Run retries in parallel
        retry_results = await asyncio.gather(*[task for _, task in retry_tasks])

        # Process retry results
        newly_failed_indices = []
        for (original_idx, _), retry_result in zip(retry_tasks, retry_results):
            if retry_result is None:
                newly_failed_indices.append(original_idx)
                chapter_downloads[original_idx] = None
                print(
                    f"   ❌ Chapter {original_idx + 1} ({selected_chapters[original_idx].get('name', 'Unknown')}) still failed after retry"
                )
            else:
                # Successfully retried - update the mapping
                chapter_downloads[original_idx] = retry_result
                print(
                    f"   ✅ Chapter {original_idx + 1} ({selected_chapters[original_idx].get('name', 'Unknown')}) downloaded on retry"
                )

        failed_indices = newly_failed_indices

    # Build final downloaded_data list in proper chapter order
    downloaded_data = []
    for i in range(len(selected_chapters)):
        if chapter_downloads[i] is not None:
            downloaded_data.append(chapter_downloads[i])

    # Check if any chapters still failed after retry
    if failed_indices:
        failed_chapter_names = [selected_chapters[i]["name"] for i in failed_indices]
        if task_manager is not None and job is not None:
            task_manager.set_status(
                job,
                "failed",
                f"Missing chapters after retry: {', '.join(failed_chapter_names[:5])}",
            )
        else:
            print(
                f"\n🛑 EPUB creation halted. {len(failed_indices)} chapter(s) failed even after retry:"
            )
            for i, ch_name in enumerate(failed_chapter_names[:10], 1):
                print(f"   {i}. {ch_name}")
            if len(failed_chapter_names) > 10:
                print(f"   ... and {len(failed_chapter_names) - 10} more")
        return False

    if task_manager is not None and job is not None:
        task_manager.set_status(job, "generating EPUB", "Writing EPUB file")
    else:
        print("\n📦 Building EPUB...")
    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from .epub_builder import create_epub

        # Attempt to fetch cover bytes using the active async session so the
        # threaded EPUB writer doesn't need to perform network I/O. This helps
        # when multiple EPUBs are being created concurrently.
        cover_bytes = None
        if cover:
            try:
                resp = await session.get(
                    cover,
                    headers=scraper.current_headers,
                    cookies=scraper.current_cookies,
                    timeout=15,
                    impersonate="chrome120",
                )
                if getattr(resp, "status_code", None) == 200:
                    # curl_cffi exposes raw bytes on `.content`
                    cover_bytes = getattr(resp, "content", None)
                else:
                    print(
                        f"⚠️ Cover download failed: {getattr(resp, 'status_code', 'unknown')}"
                    )
            except Exception as exc:
                print(f"⚠️ Could not download cover: {exc}")

        create_args = dict(
            title=title,
            author=author,
            cover_url=cover,
            cover_bytes=cover_bytes,
            description=description,
            chapters=downloaded_data,
            output_dir=out_dir,
            headers=scraper.current_headers,
            cookies=scraper.current_cookies,
            verbose=task_manager is None or job is None,
        )
        if output_filename:
            create_args["output_filename"] = output_filename

        if task_manager is not None and job is not None:
            output_path = await task_manager.run_exclusive(
                output_filename or clean_filename(title), create_epub, **create_args
            )
        else:
            output_path = create_epub(**create_args)

        if output_path:
            if job is not None:
                job.output_path = output_path
            if task_manager is not None and job is not None:
                task_manager.set_status(job, "completed", f"Saved to {output_path}")
        if task_manager is None or job is None:
            print("🧹 Cleaning up cache...", end=" ")
        for ch in selected_chapters:
            scraper.clear_cache_for_url(ch["url"])
        if task_manager is None or job is None:
            print("Done!")
        return output_path if output_path else False
    except Exception as e:
        if task_manager is None or job is None:
            print(f"\n❌ Failed to create EPUB: {e}")
        if task_manager is not None and job is not None:
            task_manager.set_status(job, "failed", str(e))
        return False


def _build_output_filename(title: str, url: str) -> str:
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return clean_filename(f"{title}_{digest}")


def _find_existing_epub_path(title: str, url: str, entry: dict = None) -> Path | None:
    candidates = []

    if entry:
        output_path = entry.get("output_path")
        if output_path:
            candidates.append(Path(output_path))

        output_filename = entry.get("output_filename")
        if output_filename:
            candidates.append(
                Path(DEFAULT_OUTPUT_DIR) / f"{clean_filename(output_filename)}.epub"
            )

    clean_title = clean_filename(title)
    candidates.append(Path(DEFAULT_OUTPUT_DIR) / f"{clean_title}.epub")
    candidates.append(
        Path(DEFAULT_OUTPUT_DIR) / f"{_build_output_filename(title, url)}.epub"
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    normalized_title = clean_title.lower()
    for candidate in Path(DEFAULT_OUTPUT_DIR).rglob("*.epub"):
        if normalized_title and normalized_title in candidate.stem.lower():
            return candidate

    return None


def _batch_output_filename(title: str, url: str) -> str:
    return _build_output_filename(title, url)


def _parse_download_targets(raw_input: str) -> list[str]:
    raw_input = raw_input.strip()
    if not raw_input:
        return []

    if "http://" in raw_input or "https://" in raw_input:
        tokens = [
            part.strip() for part in re.split(r"[\s,]+", raw_input) if part.strip()
        ]
        return build_novelbin_urls(tokens)

    if "," in raw_input or "\n" in raw_input:
        return build_novelbin_urls(raw_input)

    return [build_novelbin_url(raw_input)]


async def _prompt_chapter_indices(ainput, total_chapters: int) -> list[int]:
    if ainput is None:
        sel_str = input(f"Select chapters (Total: {total_chapters}) [Enter for ALL]: ")
    else:
        sel_str = await ainput(
            f"Select chapters (Total: {total_chapters}) [Enter for ALL]: "
        )
    return parse_chapter_selection(sel_str, total_chapters)


async def _start_background_download(
    scraper,
    lib,
    manager,
    url: str,
    title: str,
    author: str,
    cover: str,
    description: str,
    all_chapters,
    selection_indices: list[int],
    output_filename: str,
    ainput=None,
    add_to_library: bool = False,
):
    if manager is None:
        manager = ScrapeTaskManager(auto_print=False)

    async def _job_runner(job, task_manager):
        job.title = title
        task_manager.set_status(
            job, "processing", f"Downloading {len(selection_indices)} chapters"
        )
        async with AsyncSession(impersonate="chrome") as bg_session:
            success = await download_workflow(
                scraper,
                bg_session,
                title,
                author,
                cover,
                description,
                all_chapters,
                selection_indices=selection_indices,
                output_filename=output_filename,
                task_manager=task_manager,
                job=job,
                ainput=ainput,
                suppress_progress=True,
            )

            if success and add_to_library:
                lib.add_novel(title, url, len(all_chapters), output_path=success)

            return success

    # Queue through manager's concurrency-controlled start_background() instead of direct create_task()
    await manager.start_background([url], _job_runner, concurrency=2)


async def download_single_novel(scraper, lib, url: str, ainput=None, manager=None):
    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, all_chapters = (
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

        if not library_entry:
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
        title, author, cover, description, all_chapters = (
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


async def refresh_cover_mode(scraper, lib, novel_url=None, epub_path=None):
    if not novel_url:
        novel_url = input("Enter the novel URL: ").strip()

    if not novel_url:
        print("❌ No novel URL provided.")
        return

    entry = lib.get_all().get(novel_url, {})

    async with AsyncSession(impersonate="chrome") as session:
        title, author, cover, description, chapters = await scraper.extract_metadata(
            session, novel_url
        )

    target_path = (
        Path(epub_path)
        if epub_path
        else _find_existing_epub_path(title, novel_url, entry)
    )

    if not target_path:
        print(
            "❌ Could not find the existing EPUB. Pass --epub-path or place it in the default output folder."
        )
        return

    from .epub_builder import refresh_epub_cover

    success = refresh_epub_cover(target_path, cover, headers=scraper.current_headers)
    if success and entry:
        lib.add_novel(
            title,
            novel_url,
            entry.get("last_count", len(chapters)),
            output_path=target_path,
        )


async def save_raw_html(urls, out_dir: Path):
    """Fetch raw HTML for the provided URLs and save them to disk."""
    from .config import BASE_URL, HEADERS

    out_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []

    async with AsyncSession(impersonate="chrome120") as session:
        for url in urls:
            try:
                response = await session.get(
                    url,
                    headers=HEADERS,
                    timeout=30,
                    impersonate="chrome120",
                )
                html = response.text
                if not html:
                    print(f"⚠️ No content for {url}")
                    continue

                file_name = url.split("/")[-1] or "page"
                file_path = out_dir / f"{file_name}.html"
                file_path.write_text(html, encoding="utf-8")
                saved_paths.append(file_path)
                print(f"✅ Saved {url} -> {file_path}")
            except Exception as e:
                print(f"❌ Failed to save {url}: {e}")

    return saved_paths


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
            else:
                chapter_range = "no new chapters"

            updates_with_data.append((result, title, chapter_range))

        if updates_found_count == 0:
            print("\n✅ All your novels are up to date.")
            return

        # Prompt for each novel one by one (no summary list)
        print(f"\n📥 Found {updates_found_count} novel(s) with updates\n")
        selected_downloads = []

        for result, title, chapter_range in updates_with_data:
            novel_data, new_indices, current_chapters = result

            if interactive:
                if ainput is None:
                    choice = (
                        input(f"   📖 {title} {chapter_range}\n   Download? [Y/n]: ")
                        .strip()
                        .lower()
                    )
                else:
                    choice_raw = await ainput(
                        f"   📖 {title} {chapter_range}\n   Download? [Y/n]: "
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
    parser.add_argument(
        "--url",
        "--urls",
        nargs="+",
        dest="urls",
        help="Scrape one or more novel names or URLs",
    )
    parser.add_argument(
        "--save-html",
        nargs="+",
        help="Save raw HTML of given novel names or URLs (space-separated)",
    )
    parser.add_argument(
        "--refresh-cover",
        help="Refresh the cover image for an existing novel EPUB using its novel URL",
    )
    parser.add_argument(
        "--epub-path",
        help="Optional explicit EPUB path to update when refreshing a cover",
    )
    args = parser.parse_args()

    from .library import LibraryManager
    from .scraper import AsyncScraper

    scraper = AsyncScraper()
    lib = LibraryManager()
    manager = ScrapeTaskManager(auto_print=False)
    monitor_task = None

    if args.save_html:
        args.save_html = build_novelbin_urls(args.save_html)
        samples_dir = Path(DEFAULT_OUTPUT_DIR) / "samples"
        print(f"Saving raw HTML to {samples_dir}...")
        saved = await save_raw_html(args.save_html, samples_dir)
        if saved:
            print("Saved files:")
            for p in saved:
                print(" -", p)
        else:
            print("No files saved.")
        return

    if args.urls:
        urls = build_novelbin_urls(args.urls)
        if not urls:
            print("❌ No valid URL or novel name provided.")
            return

        if len(urls) == 1:
            await download_single_novel(scraper, lib, urls[0], manager=manager)
        else:
            await scrape_multiple_urls(scraper, lib, urls)
        return

    if args.query:
        await search_mode(scraper, lib, args.query, manager=manager)
        return

    if args.refresh_cover:
        await refresh_cover_mode(scraper, lib, args.refresh_cover, args.epub_path)
        return

    loop = asyncio.get_event_loop()

    async def ainput(prompt: str = "") -> str:
        return await loop.run_in_executor(None, input, prompt)

    async def _monitor_loop():
        try:
            while True:
                jobs = manager.list_jobs()
                if jobs:
                    print("\n--- Jobs ---")
                    for index, job in enumerate(jobs, start=1):
                        status_line = f"{index}. {job.label} — {job.status}"
                        if job.error:
                            status_line += f" ({job.error})"
                        print(status_line)
                    print("--- End Jobs ---\n")
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return

    print("\nInteractive CLI — type 'help' for commands")

    while True:
        try:
            cmd = (await ainput("novelbin> ")).strip()
        except (EOFError, KeyboardInterrupt):
            cmd = "exit"

        if not cmd:
            continue

        parts = cmd.split(None, 1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command in ("q", "quit", "exit"):
            break

        if command in ("help", "h", "?"):
            print(
                """
📚 Available Commands:
  search <query>           - Search for a novel and download chapters
  update                   - Check all library novels for new chapters
  download <name-or-url>   - Download a novel by name or URL (comma-separated for multiple)
  cover <url|path>         - Refresh cover image for an existing EPUB
  status                   - Show active scrape/download jobs
  cancel <job#|url>        - Cancel a job by index or URL
  watch                    - Monitor job progress in real-time
  stopwatch                - Stop the job monitor
  library                  - List all novels in your library
  exit                     - Exit the CLI
                """
            )
            continue

        if command == "search":
            if not arg:
                print("Usage: search <query>")
                continue
            # Run search interactively - download starts in background after selection
            await search_mode(scraper, lib, arg, ainput, manager)
            continue

        if command == "update":
            # Run entire update flow in foreground: check → prompt selection → queue downloads
            await check_updates(scraper, lib, manager, interactive=True, ainput=ainput)
            continue

        if command == "download":
            if not arg:
                print("Usage: download <name-or-url>[,...]")
                continue
            targets = _parse_download_targets(arg)
            targets = [target for target in targets if target]

            if not targets:
                print("No valid URL produced.")
                continue

            if len(targets) == 1:
                # Run single download interactively - actual download starts in background after selection
                await download_single_novel(scraper, lib, targets[0], ainput, manager)
            else:
                print(f"Queuing {len(targets)} scrape jobs in background...")

                async def background_runner(job, task_manager):
                    return await scrape_direct_url_job(
                        scraper, lib, job.url, job, task_manager, ainput
                    )

                # Start background run but don't await it to keep CLI responsive
                asyncio.create_task(
                    manager.start_background(targets, background_runner)
                )
                print("Background scrape started. Use 'status' or 'watch' to monitor.")
            continue

        if command == "status":
            jobs = manager.list_jobs()
            if not jobs:
                print("No jobs yet.")
                continue
            for index, job in enumerate(jobs, start=1):
                if job.message:
                    print(f"{index}. {job.label} — {job.status}: {job.message}")
                else:
                    print(f"{index}. {job.label} — {job.status}")
            continue

        if command == "completed":
            jobs = manager.list_jobs()
            completed = [j for j in jobs if j.status == "completed"]
            if not completed:
                print("No completed jobs yet.")
                continue
            for index, job in enumerate(completed, start=1):
                out = job.output_path or "(no output path)"
                print(f"{index}. {job.label} — completed -> {out}")
            continue

        if command == "watch":
            if monitor_task and not monitor_task.done():
                print("Monitor already running.")
                continue
            monitor_task = asyncio.create_task(_monitor_loop())
            print("Started job monitor (updates every 2s). Use 'stopwatch' to stop.")
            continue

        if command == "stopwatch":
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
                monitor_task = None
                print("Stopped job monitor.")
            else:
                print("No monitor running.")
            continue

        if command == "cancel":
            if not arg:
                print("Usage: cancel <job_index|url>")
                continue

            if arg.isdigit():
                index = int(arg) - 1
                jobs = manager.list_jobs()
                if 0 <= index < len(jobs):
                    ok = manager.cancel_url(jobs[index].url)
                    print("Cancelled" if ok else "Not found")
                else:
                    print("Invalid job index")
            else:
                ok = manager.cancel_url(arg)
                print("Cancelled" if ok else "Not found")
            continue

        if command == "cover":
            if not arg:
                print("Usage: cover <novel-url> or cover <epub-file-path>")
                continue
            # Could be a URL or file path
            if "http" in arg:
                # It's a URL
                await refresh_cover_mode(scraper, lib, novel_url=arg)
            else:
                # It's a file path
                await refresh_cover_mode(scraper, lib, epub_path=arg)
            continue

        if command == "library":
            entries = lib.get_all()
            if not entries:
                print("Library empty.")
            else:
                for url, data in entries.items():
                    print(f"- {data.get('title','Unknown')} -> {url}")
            continue

        print("Unknown command. Type 'help' for available commands.")


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


def start():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")


if __name__ == "__main__":
    start()
