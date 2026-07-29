# main.py
import asyncio
import hashlib
import json
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession  # <--- CHANGED: No more aiohttp

# Ensure LIBRARY_FILE is defined in your config alongside the others
from .config import DEFAULT_OUTPUT_DIR, LIBRARY_FILE
from .downloader import download_workflow
from .task_manager import ScrapeTaskManager
from .utils import (
    build_novelbin_url,
    build_novelbin_urls,
    clean_filename,
    parse_chapter_selection,
)

# Initialize the logger for this module
logger = logging.getLogger(__name__)


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


async def _monitor_loop():
    manager = ScrapeTaskManager(auto_print=False)
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


def _set_epub_version(book):
    if hasattr(book, "set_version"):
        try:
            book.set_version("3.0")
            return
        except Exception:
            pass
    book.version = "3.0"


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_ns(tag: str) -> str:
    """Helper to strip XML namespaces from tags for reliable parsing."""
    return tag.split("}")[-1] if "}" in tag else tag


def _resolve_library_url(target_url: str) -> Path:
    """
    Checks if a URL exists in LIBRARY_FILE and returns the associated EPUB path.
    """
    library_path = Path(LIBRARY_FILE)
    if not library_path.exists():
        logger.error(f"Library file missing at {library_path}")
        return None

    try:
        with open(library_path, "r", encoding="utf-8") as f:
            library_data = json.load(f)

        if isinstance(library_data, list):
            for entry in library_data:
                if entry.get("url") == target_url:
                    path_str = entry.get("file_path")
                    if path_str:
                        return Path(path_str)

        elif isinstance(library_data, dict):
            if target_url in library_data:
                entry = library_data[target_url]
                path_str = entry.get("file_path") if isinstance(entry, dict) else entry
                if path_str:
                    return Path(path_str)

    except json.JSONDecodeError:
        logger.error("Library file is not valid JSON.")
    except Exception as e:
        logger.error(f"Failed to read library file: {e}")

    return None


# =========================================================
# CORE WORKER FUNCTIONS (SYNCHRONOUS)
# =========================================================


def _extract_epub_data(epub_path: Path):
    """
    Extracts complete metadata, chapters, and existing cover image
    directly from an EPUB zip archive using its OPF manifest.
    """
    logger.info(f"Extracting data dynamically from EPUB archive: {epub_path}")

    metadata = {
        "title": epub_path.stem,
        "author": "",
        "description": "",
        "publisher": "",
        "language": "en",
        "subjects": [],
    }
    chapters = []
    cover_bytes = None

    with zipfile.ZipFile(epub_path, "r") as z:
        # 1. Locate the OPF file via META-INF/container.xml
        try:
            container_data = z.read("META-INF/container.xml")
            container_root = ET.fromstring(container_data)

            opf_path = None
            for elem in container_root.iter():
                if _strip_ns(elem.tag) == "rootfile":
                    opf_path = elem.attrib.get("full-path")
                    break

            if not opf_path:
                raise ValueError("No OPF file designated in container.xml")
        except KeyError:
            raise ValueError(
                "META-INF/container.xml is missing; invalid EPUB structure."
            )

        opf_dir = posixpath.dirname(opf_path)

        # 2. Parse the OPF manifest
        opf_data = z.read(opf_path)
        opf_root = ET.fromstring(opf_data)

        # 3. Extract Extended Metadata (Dublin Core tags)
        for elem in opf_root.iter():
            tag = _strip_ns(elem.tag)

            if not elem.text:
                continue

            if tag == "title":
                metadata["title"] = elem.text
            elif tag == "creator":
                metadata["author"] = elem.text
            elif tag == "description":
                metadata["description"] = elem.text
            elif tag == "publisher":
                metadata["publisher"] = elem.text
            elif tag == "language":
                metadata["language"] = elem.text
            elif tag == "subject":
                metadata["subjects"].append(elem.text)

        # 4. Map the Manifest (id -> href) and locate Cover Image
        manifest = {}
        cover_id = None

        for item in opf_root.iter():
            if _strip_ns(item.tag) == "item":
                item_id = item.attrib.get("id")
                href = item.attrib.get("href")
                if item_id and href:
                    manifest[item_id] = href

                # Check EPUB 3 cover properties
                if item.attrib.get("properties") == "cover-image":
                    cover_id = item_id

        # Fallback for EPUB 2 cover metadata
        if not cover_id:
            for meta in opf_root.iter():
                if _strip_ns(meta.tag) == "meta" and meta.attrib.get("name") == "cover":
                    cover_id = meta.attrib.get("content")
                    break

        if cover_id and cover_id in manifest:
            cover_href = manifest[cover_id]
            cover_zip_path = (
                posixpath.join(opf_dir, cover_href) if opf_dir else cover_href
            )
            try:
                cover_bytes = z.read(cover_zip_path)
                logger.info(f"Extracted existing cover image: {cover_zip_path}")
            except KeyError:
                logger.warning(
                    f"Cover image declared but missing in archive: {cover_zip_path}"
                )

        # 5. Read the Spine to determine Chapter sequence
        spine_ids = []
        for itemref in opf_root.iter():
            if _strip_ns(itemref.tag) == "itemref":
                idref = itemref.attrib.get("idref")
                if idref:
                    spine_ids.append(idref)

        # 6. Extract Chapter Content
        for item_id in spine_ids:
            if item_id not in manifest:
                continue

            href = manifest[item_id]
            href_lower = href.lower()

            # Explicitly ignore structure-only HTML files
            if "nav" in href_lower or "cover" in href_lower:
                continue

            chapter_zip_path = posixpath.join(opf_dir, href) if opf_dir else href

            try:
                chapter_data = z.read(chapter_zip_path)
                soup = BeautifulSoup(chapter_data, "html.parser")
                body = soup.find("body")

                title_tag = soup.find("title")
                chapter_title = (
                    title_tag.text.strip()
                    if title_tag and title_tag.text
                    else f"Chapter {len(chapters) + 1}"
                )

                if body:
                    chapter_content = "".join(str(child) for child in body.contents)
                else:
                    chapter_content = chapter_data.decode("utf-8", "ignore")

                chapters.append({"title": chapter_title, "content": chapter_content})
            except Exception as e:
                logger.warning(f"Failed to process chapter {chapter_zip_path}: {e}")

    return metadata, chapters, cover_bytes
