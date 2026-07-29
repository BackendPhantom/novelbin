import asyncio
from pathlib import Path

from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

from .config import DEFAULT_OUTPUT_DIR
from .utils import clean_filename, parse_chapter_selection


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
        from .epub_builder import build_epub_async

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

        required_keys = (
            "title",
            "author",
            "chapters",
            "description",
            "cover_url",
            "cover_bytes",
        )
        epub_args = {k: create_args[k] for k in required_keys}
        if task_manager is not None and job is not None:
            output_path = await task_manager.run_exclusive(
                output_filename or clean_filename(title), build_epub_async, **epub_args
            )
        else:
            output_path = await build_epub_async(**epub_args)

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
