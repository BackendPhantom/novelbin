# main.py
import argparse
import asyncio
from pathlib import Path

from .config import DEFAULT_OUTPUT_DIR
from .downloader import save_raw_html
from .helpers import _parse_download_targets
from .orchestrator import (
    check_updates,
    download_single_novel,
    scrape_direct_url_job,
    scrape_multiple_urls,
)
from .library_sync import refresh_cover_mode

from .search import search_mode
from .task_manager import ScrapeTaskManager
from .utils import build_novelbin_urls


async def run_cli():
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
    # --- ADDED: Offline flag ---
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run operations in offline mode (local only, no network)",
    )
    args = parser.parse_args()

    from .library import LibraryManager
    from .scraper import AsyncScraper

    scraper = AsyncScraper()
    lib = LibraryManager()
    manager = ScrapeTaskManager(auto_print=False)
    monitor_task = None

    asyncio.create_task(
        scraper._cleanup_cache_async(max_age_hours=48, max_size_mb=1024)
    )

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
        # --- ADDED: Mode selection ---
        mode = "offline" if args.offline else "online"
        
        # Dynamically determine if the argument is a URL, File Path, or Name
        if "http" in args.refresh_cover:
            await refresh_cover_mode(scraper, lib, url=args.refresh_cover, path=args.epub_path, mode=mode)
        elif args.refresh_cover.endswith(".epub") or "/" in args.refresh_cover or "\\" in args.refresh_cover:
            await refresh_cover_mode(scraper, lib, path=args.refresh_cover, mode=mode)
        else:
            await refresh_cover_mode(scraper, lib, name=args.refresh_cover, path=args.epub_path, mode=mode)
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
            print("""
  Available Commands:
  search <query>           - Search for a novel and download chapters
  update                   - Check all library novels for new chapters
  download <name-or-url>   - Download a novel by name or URL (comma-separated for multiple)
  meta <url|path> [--offline] - Refresh metadata/cover for an EPUB or batch folder
  status                   - Show active scrape/download jobs
  cancel <job#|url>        - Cancel a job by index or URL
  watch                    - Monitor job progress in real-time
  stopwatch                - Stop the job monitor
  library                  - List all novels in your library
  exit                     - Exit the CLI
                """)
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

        if command == "meta":
            if not arg:
                print("Usage: meta <novel-url> | <epub-file-path> | <folder-path> [--offline]")
                continue
            
            # --- ADDED: Parse offline flag from interactive command ---
            mode = "online"
            if arg.endswith("--offline"):
                mode = "offline"
                arg = arg.replace("--offline", "").strip()

            # Route the input to the correct keyword argument
            if "http" in arg:
                await refresh_cover_mode(scraper, lib, url=arg, mode=mode)
            elif arg.endswith(".epub") or "/" in arg or "\\" in arg:
                await refresh_cover_mode(scraper, lib, path=arg, mode=mode)
            else:
                await refresh_cover_mode(scraper, lib, name=arg, mode=mode)
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
