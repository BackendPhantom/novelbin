import asyncio
import logging
from pathlib import Path
from curl_cffi import requests
from curl_cffi.requests import AsyncSession
from ebooklib import epub

# Ensure LIBRARY_FILE is defined in your config alongside the others
from .config import CSS_STYLE, DEFAULT_OUTPUT_DIR
from .scraper import AsyncScraper
from .utils import build_novelbin_url, clean_filename
from bs4 import BeautifulSoup
import re

# Initialize the logger for this module
logger = logging.getLogger(__name__)

# =========================================================
# HELPER FUNCTIONS
# =========================================================
from .helpers import (
    _extract_epub_data,
    _normalize_whitespace,
    _resolve_library_url,
    _set_epub_version,
    _format_chapter_html,
)


def create_epub(
    title: str,
    author: str,
    chapters: list,
    description: str = "",
    publisher: str = "",
    language: str = "en",
    subjects: list = None,
    cover_url: str = None,
    cover_bytes: bytes = None,
    output_dir: Path = None,  # <--- Added support for custom output directories
) -> str:
    """
    Synchronous worker for generating EPUBs.
    """
    logger.info(f"Building EPUB for '{title}'...")
    try:
        book = epub.EpubBook()
        _set_epub_version(book)
        book.set_identifier(clean_filename(title))
        book.set_title(title)
        book.set_language(language or "en")

        if author:
            book.add_author(author)
        if description:
            book.add_metadata("DC", "description", description)
        if publisher:
            book.add_metadata("DC", "publisher", publisher)
        if subjects:
            for subject in subjects:
                book.add_metadata("DC", "subject", subject)

        # ----------------------------------------------------------
        # Cover
        # ----------------------------------------------------------
        if cover_url or cover_bytes:
            try:
                content = cover_bytes
                ext = "jpg"
                if cover_url:
                    ext = cover_url.rsplit(".", 1)[-1].lower()
                    if ext not in {"jpg", "jpeg", "png"}:
                        ext = "jpg"

                # Fallback to network if no secure bytes were passed
                if not content and cover_url:
                    res = requests.get(
                        cover_url,
                        impersonate="chrome150",
                        timeout=15,
                    )
                    if res.status_code == 200:
                        content = res.content
                if content:
                    book.set_cover(f"cover.{ext}", content)
            except Exception as e:
                logger.warning(f"Failed to set/download cover: {e}")

        # ----------------------------------------------------------
        # CSS
        # ----------------------------------------------------------
        style = epub.EpubItem(
            uid="style",
            file_name="style/nav.css",
            media_type="text/css",
            content=CSS_STYLE,
        )
        book.add_item(style)

        # ----------------------------------------------------------
        # Chapters
        # ----------------------------------------------------------
        epub_chapters = []
        for index, chapter in enumerate(chapters, start=1):

            # Delegate all dictionary extraction and HTML manipulation to the helper
            processed_body, final_title = _format_chapter_html(chapter, index)

            epub_chapter = epub.EpubHtml(
                title=final_title,
                file_name=f"chapter_{index:04d}.xhtml",
                lang=language or "en",
            )
            epub_chapter.content = processed_body
            epub_chapter.add_item(style)
            book.add_item(epub_chapter)
            epub_chapters.append(epub_chapter)

        logger.info(f"Added {len(epub_chapters)} chapters.")

        # ----------------------------------------------------------
        # Navigation
        # ----------------------------------------------------------
        book.toc = tuple(epub_chapters)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = epub_chapters

        # ----------------------------------------------------------
        # Output
        # ----------------------------------------------------------
        # Dynamically route the output directory so batch files stay in their original folder
        resolved_out_dir = output_dir if output_dir else DEFAULT_OUTPUT_DIR
        resolved_out_dir.mkdir(parents=True, exist_ok=True)
        epub_path = resolved_out_dir / f"{clean_filename(title)}.epub"

        epub.write_epub(str(epub_path), book)
        logger.info(f"Successfully created EPUB: {epub_path}")
        return str(epub_path)
    except Exception:
        logger.exception("Failed to build EPUB.")
        return None


def update_epub_metadata(
    name: str = None,
    path: str = None,
    url: str = None,
    new_title: str = None,
    new_author: str = None,
    new_description: str = None,
    new_publisher: str = None,
    new_subjects: list = None,
    new_cover_url: str = None,
    new_cover_bytes: bytes = None,
) -> str:
    """
    Synchronous worker for updating an EPUB's entire metadata and cover.
    It extracts the existing data natively from the zip, merges it with any
    new metadata provided, and rebuilds it.
    """
    target_path = None

    # 1. Resolve Target EPUB Based on Provided Inputs
    if name:
        cleaned_name = clean_filename(name)
        # Find a matching EPUB regardless of case
        target_path = next(
            (
                p
                for p in DEFAULT_OUTPUT_DIR.glob("*.epub")
                if p.stem.lower() == cleaned_name.lower()
            ),
            None,
        )
        if target_path is None:
            logger.warning(
                f"File '{cleaned_name}.epub' not found in default output directory."
            )
            raise FileNotFoundError(
                f"Missing '{name}'. Please provide a direct 'path' or 'url'."
            )
    elif path:
        target_path = Path(path)
        if not target_path.exists():
            raise FileNotFoundError(f"Provided path does not exist: {path}")
    elif url:
        target_path = _resolve_library_url(url)
        if not target_path or not target_path.exists():
            raise ValueError(
                f"URL '{url}' not found in the library, or the mapped file is missing."
            )
    else:
        raise ValueError(
            "You must provide either 'name', 'path', or 'url' to locate the EPUB."
        )

    # 2. Extract Existing Local Data
    try:
        metadata, chapters, existing_cover_bytes = _extract_epub_data(target_path)
    except Exception as e:
        logger.exception(f"Failed to extract data from {target_path}: {e}")
        return None

    # 3. Merge Metadata (New overrides take precedence over the ZIP's existing data)
    final_title = new_title if new_title else metadata.get("title")
    final_author = new_author if new_author else metadata.get("author")
    final_description = (
        new_description if new_description else metadata.get("description")
    )
    final_publisher = new_publisher if new_publisher else metadata.get("publisher")
    final_subjects = new_subjects if new_subjects else metadata.get("subjects")

    # Determine the cover strategy: Prioritize raw bytes, then a new URL, then the existing extracted cover
    final_cover_bytes = (
        new_cover_bytes
        if new_cover_bytes
        else (existing_cover_bytes if not new_cover_url else None)
    )

    # 4. Rebuild EPUB
    logger.info(f"Rebuilding EPUB for '{final_title}' with updated metadata...")
    new_epub_path = create_epub(
        title=final_title,
        author=final_author,
        description=final_description,
        publisher=final_publisher,
        language=metadata.get("language"),
        subjects=final_subjects,
        chapters=chapters,
        cover_url=new_cover_url,
        cover_bytes=final_cover_bytes,
        output_dir=(
            target_path.parent if target_path else None
        ),  # <--- Keeps the file in the batch folder
    )

    if new_epub_path:
        logger.info(f"  Successfully rebuilt EPUB with new metadata: {new_epub_path}")
    return new_epub_path


# =========================================================
# ASYNC WRAPPERS (USE THESE IN YOUR EVENT LOOP)
# =========================================================


async def build_epub_async(
    title: str,
    author: str,
    chapters: list,
    description: str = "",
    publisher: str = "",
    language: str = "en",
    subjects: list = None,
    cover_url: str = None,
    cover_bytes: bytes = None,
    output_dir: Path = None,
) -> str:
    """
    Safely executes the synchronous EPUB builder in a background ThreadPool.
    """
    try:
        return await asyncio.to_thread(
            create_epub,
            title=title,
            author=author,
            chapters=chapters,
            description=description,
            publisher=publisher,
            language=language,
            subjects=subjects,
            cover_url=cover_url,
            cover_bytes=cover_bytes,
            output_dir=output_dir,
        )
    except Exception as e:
        logger.error(f"Failed to complete async EPUB build for '{title}': {e}")
        return None


async def update_epub_metadata_async(
    name: str = None,
    path: str = None,
    url: str = None,
    new_title: str = None,
    new_author: str = None,
    new_description: str = None,
    new_publisher: str = None,
    new_subjects: list = None,
    new_cover_url: str = None,
    new_cover_bytes: bytes = None,
) -> str:
    """
    Safely executes the metadata updater in a background ThreadPool.
    If 'new_title' is not provided, forces a fresh scrape of metadata from the live URL.
    If no URL is provided, it derives one from the name/path. If the page fails to load, it aborts.
    """
    scraped_title = scraped_author = scraped_cover_url = scraped_description = None

    # <--- Added: Bypass the inner scrape if the caller (e.g., library_sync.py) already provided the metadata
    if not new_title:
        # 1. Generate URL if not explicitly provided
        if not url:
            if name:
                base_name = name
            elif path:
                base_name = Path(path).stem
            else:
                return "Error: You must provide either 'name', 'path', or 'url' to locate the EPUB."

            url = build_novelbin_url(base_name)
            logger.info(f"Auto-generated URL from name/path: {url}")

        # 2. Verify URL and Scrape Fresh Metadata
        logger.info(f"Fetching fresh metadata for {url} using AsyncScraper...")
        scraper = AsyncScraper()
        async with AsyncSession(impersonate="chrome150") as session:
            # _fetch_url handles Cloudflare and returns None if it fails or 404s
            html = await scraper._fetch_url(session, url)
            if not html:
                logger.error(f"Failed to reach {url} (Non-200 or Cloudflare block).")
                return (
                    f"Automatic URL resolution failed for '{url}'. The page might not exist, "
                    f"or it is heavily protected. Please provide the exact 'url' to the novel."
                )

            # If successful, extract metadata from the live site
            meta = await scraper.extract_metadata(session, url)
            if meta:
                # Unpack the 5-item tuple, discarding API chapters (_) to preserve local ones
                (
                    scraped_title,
                    scraped_author,
                    scraped_cover_url,
                    scraped_description,
                    _,
                ) = meta
            else:
                return f"Failed to extract metadata from '{url}'. The page layout might have changed."

    # 3. Rebuild EPUB via Synchronous Worker
    try:
        # Offload the heavy ZIP extraction and rebuilding to a thread.
        # Scraped data overrides the local ZIP data natively inside update_epub_metadata.
        return await asyncio.to_thread(
            update_epub_metadata,
            name=name,
            path=path,
            url=url,
            new_title=new_title or scraped_title,
            new_author=new_author or scraped_author,
            new_description=new_description or scraped_description,
            new_publisher=new_publisher,
            new_subjects=new_subjects,
            new_cover_url=new_cover_url or scraped_cover_url,
            new_cover_bytes=new_cover_bytes,
        )
    except Exception as e:
        logger.error(f"Failed to complete async metadata update: {e}")
        return f"An error occurred while rebuilding the EPUB: {e}"
