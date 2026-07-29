from html import escape
import re
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests
from ebooklib import epub

from .config import CSS_STYLE
from .nav_builder import _build_epub3_nav
from .utils import clean_filename


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


def _sanitize_chapter_title(raw_title: str, idx: int) -> str:
    chapter_no = f"{idx:02d}"
    cleaned = _normalize_whitespace(raw_title)

    if not cleaned:
        return f"Chapter {chapter_no}"

    cleaned = re.sub(
        r"^\s*(chapter|ch|chap)\s*\d*\s*[-:.)]*\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    if not cleaned:
        return f"Chapter {chapter_no}"

    return f"Chapter {chapter_no} - {cleaned}"


def _strip_duplicate_heading(content: str, title: str) -> str:
    if not content:
        return ""

    soup = BeautifulSoup(content, "html.parser")
    normalized_title = _normalize_whitespace(title).lower()
    chapter_suffix = (
        normalized_title.split(" - ", 1)[1]
        if " - " in normalized_title
        else normalized_title
    )

    for tag in soup.select("h1, h2, h3, h4, .chapter-title, .title"):
        text = _normalize_whitespace(tag.get_text(" ", strip=True)).lower()
        if text and (text == normalized_title or text == chapter_suffix):
            tag.decompose()

    body = soup.body
    if body is not None:
        return body.decode_contents().strip()
    return soup.decode_contents().strip()


def _wrap_chapter_xhtml(title: str, content: str) -> str:
    body_content = _strip_duplicate_heading(content, title)
    return f"""<section epub:type=\"chapter\">
    <h1 class=\"chapter-title\">{escape(title)}</h1>
    <div class=\"chapter-content\">
        {body_content}
    </div>
</section>"""


def create_epub(
    title: str,
    author: str,
    cover_url: str,
    description: str,
    chapters: list,
    output_dir: Path,
    output_filename: str = None,
    headers: dict = None,
    cookies: dict = None,
    verbose: bool = True,
    cover_bytes: bytes = None,
):
    book = epub.EpubBook()
    _set_epub_version(book)

    # Metadata
    book.set_identifier(clean_filename(title))
    book.set_title(title)
    book.set_language("en")
    if description:
        book.add_metadata("DC", "description", description)
    if author:
        book.add_author(author)

    # Style
    css_item = epub.EpubItem(
        uid="style_css",
        file_name="style.css",
        media_type="text/css",
        content=CSS_STYLE,
    )
    book.add_item(css_item)

    # Cover: prefer pre-fetched bytes if provided to avoid synchronous
    # network calls inside the EPUB writer thread.
    if cover_bytes:
        try:
            book.set_cover("cover.jpg", cover_bytes)
            if verbose:
                print("✅ Cover added to EPUB (prefetched)")
        except Exception as e:
            print(f"⚠️ Could not apply prefetched cover: {e}")
    elif cover_url:
        try:
            if verbose:
                print("🖼️ Downloading cover...")
            res = requests.get(
                cover_url,
                headers=headers or {},
                cookies=cookies or {},
                timeout=15,
                impersonate="chrome120",
            )
            if res.status_code == 200:
                book.set_cover("cover.jpg", res.content)
                if verbose:
                    print("✅ Cover added to EPUB")
            else:
                print(f"⚠️ Cover download failed: {res.status_code}")
        except Exception as e:
            print(f"⚠️ Could not download cover: {e}")

    # Chapters
    epub_chapters = []
    toc_links = []

    for idx, ch in enumerate(chapters, start=1):
        raw_title = ch.get("title") or ch.get("name") or ""
        chapter_title = _sanitize_chapter_title(raw_title, idx)
        chapter_content = ch.get("content", "")
        chapter_file = f"chapter_{idx:04d}.xhtml"

        c_item = epub.EpubHtml(
            uid=f"chapter_{idx:04d}",
            title=chapter_title,
            file_name=chapter_file,
            lang="en",
        )
        c_item.content = _wrap_chapter_xhtml(chapter_title, chapter_content)
        c_item.add_item(css_item)

        book.add_item(c_item)
        epub_chapters.append(c_item)
        toc_links.append(epub.Link(chapter_file, chapter_title, c_item.get_id()))

    # EPUB 3 navigation: keep TOC internal, not as a spine page
    book.toc = tuple(toc_links)
    book.spine = epub_chapters
    book.add_item(epub.EpubNcx())

    nav_item = epub.EpubHtml(
        uid="nav",
        file_name="nav.xhtml",
        title="Table of Contents",
        lang="en",
    )
    nav_item.content = _build_epub3_nav(title, epub_chapters)
    book.add_item(nav_item)

    # Write - use temp file to ensure atomic write (avoids suffix issues)
    import tempfile

    safe_title = clean_filename((output_filename or title).strip())
    output_path = output_dir / f"{safe_title}.epub"

    # Write to temporary file first, then move to final location
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".epub", dir=str(output_dir)
    ) as tmp:
        temp_path = Path(tmp.name)

    try:
        epub.write_epub(str(temp_path), book)
        # Delete existing file if present, then move temp file
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        temp_path.replace(output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)

    if verbose:
        print(f"\n✅ EPUB successfully saved to: {output_path}")
    return output_path


def refresh_epub_cover(epub_path: Path, cover_url: str, headers: dict = None) -> bool:
    """Downloads a cover image and injects it into an existing EPUB file without re-downloading the entire content."""
    import tempfile
    import zipfile
    import xml.etree.ElementTree as ET
    import mimetypes

    epub_path = Path(epub_path)

    if not epub_path.exists():
        print(f"❌ EPUB not found: {epub_path}")
        return False

    if not cover_url:
        print("❌ No cover URL available to download.")
        return False

    try:
        print("🖼️ Downloading cover...")
        req_headers = headers if headers else {}
        res = requests.get(
            cover_url,
            headers=req_headers,
            timeout=15,
            impersonate="chrome120",
        )
        if res.status_code != 200 or not res.content:
            print(f"⚠️ Cover download failed: {res.status_code}")
            return False
    except Exception as exc:
        print(f"⚠️ Could not download cover: {exc}")
        return False

    # Determine media type and filename
    media_type = mimetypes.guess_type(cover_url)[0] or "image/jpeg"
    if not media_type.startswith("image/"):
        media_type = res.headers.get("content-type", "image/jpeg")

    extension = mimetypes.guess_extension(media_type or "")
    if not extension:
        extension = ".jpg"
    cover_filename = f"cover{extension}"

    # Patch the EPUB to include the cover
    OPF_NS = "http://www.idpf.org/2007/opf"
    DC_NS = "http://purl.org/dc/elements/1.1/"
    ET.register_namespace("", OPF_NS)
    ET.register_namespace("dc", DC_NS)

    try:
        with zipfile.ZipFile(epub_path, "r") as source_zip:
            opf_name = next(
                (
                    name
                    for name in source_zip.namelist()
                    if name.endswith("content.opf")
                ),
                None,
            )
            if not opf_name:
                print("❌ Could not locate content.opf inside the EPUB.")
                return False

            opf_dir = Path(opf_name).parent
            cover_href = (opf_dir / cover_filename).as_posix()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as temp_file:
                temp_path = Path(temp_file.name)

            try:
                with zipfile.ZipFile(temp_path, "w") as target_zip:
                    for info in source_zip.infolist():
                        if info.filename == opf_name:
                            # Patch the OPF to reference the new cover
                            opf_root = ET.fromstring(source_zip.read(info.filename))
                            metadata = opf_root.find(f"{{{OPF_NS}}}metadata")
                            manifest = opf_root.find(f"{{{OPF_NS}}}manifest")

                            if metadata is None or manifest is None:
                                raise ValueError("Invalid EPUB package file")

                            # Update or add cover item in manifest
                            cover_id = "cover-image"
                            cover_item = None
                            for item in manifest.findall(f"{{{OPF_NS}}}item"):
                                if (
                                    item.get("id") == cover_id
                                    or item.get("href") == cover_href
                                ):
                                    cover_item = item
                                    break

                            if cover_item is None:
                                cover_item = ET.SubElement(
                                    manifest,
                                    f"{{{OPF_NS}}}item",
                                    {
                                        "id": cover_id,
                                        "href": cover_href,
                                        "media-type": media_type,
                                    },
                                )
                            else:
                                cover_item.set("id", cover_id)
                                cover_item.set("href", cover_href)
                                cover_item.set("media-type", media_type)

                            # Ensure metadata has cover reference
                            meta_item = None
                            for meta in metadata.findall(f"{{{OPF_NS}}}meta"):
                                if meta.get("name") == "cover":
                                    meta_item = meta
                                    break

                            if meta_item is None:
                                ET.SubElement(
                                    metadata,
                                    f"{{{OPF_NS}}}meta",
                                    {"name": "cover", "content": cover_id},
                                )
                            else:
                                meta_item.set("content", cover_id)

                            patched_opf = ET.tostring(
                                opf_root, encoding="utf-8", xml_declaration=True
                            )
                            target_zip.writestr(info, patched_opf)
                        elif info.filename in {
                            cover_href,
                            "cover.jpg",
                            "cover.jpeg",
                            "cover.png",
                        }:
                            # Skip old cover files
                            continue
                        else:
                            target_zip.writestr(info, source_zip.read(info.filename))

                    # Add the new cover file
                    target_zip.writestr(cover_href, res.content)

                temp_path.replace(epub_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

        print(f"✅ Cover updated: {epub_path}")
        return True
    except Exception as exc:
        print(f"❌ Failed to update EPUB cover: {exc}")
        return False
