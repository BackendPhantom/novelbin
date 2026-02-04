# epub_builder.py
from pathlib import Path

from curl_cffi import requests
from ebooklib import epub

from .config import CSS_STYLE
from .utils import clean_filename


def create_epub(
    title: str,
    author: str,
    cover_url: str,
    description: str,
    chapters: list,
    output_dir: Path,
    headers: dict = None,  # <--- NEW ARGUMENT
):
    book = epub.EpubBook()

    # Metadata
    book.set_identifier(clean_filename(title))
    book.set_title(title)
    book.set_language("en")
    book.add_metadata("DC", "description", description)
    book.add_author(author)

    # Style
    css_item = epub.EpubItem(
        uid="style_css", file_name="style.css", media_type="text/css", content=CSS_STYLE
    )
    book.add_item(css_item)

    # Cover
    if cover_url:
        try:
            print("🖼️ Downloading cover...")
            # Use the PASSED headers (with cookies), or fallback to empty dict
            req_headers = headers if headers else {}

            res = requests.get(
                cover_url,
                headers=req_headers,  # <--- USE THE COOKIES
                timeout=15,
                impersonate="chrome120",
            )
            if res.status_code == 200:
                book.set_cover("cover.jpg", res.content)
            else:
                print(f"⚠️ Cover download failed: {res.status_code}")
        except Exception as e:
            print(f"⚠️ Could not download cover: {e}")

    # Chapters
    epub_chapters = []
    for idx, ch in enumerate(chapters, start=1):
        chapter_content = f"""
            <h2>{ch['title']}</h2>
            {ch['content']}
        """

        c_item = epub.EpubHtml(
            title=ch["title"], file_name=f"chapter_{idx}.xhtml", lang="en"
        )
        c_item.content = chapter_content
        c_item.add_item(css_item)

        book.add_item(c_item)
        epub_chapters.append(c_item)

    # Structure
    book.toc = tuple(epub_chapters)
    book.spine = ["nav", *epub_chapters]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write
    safe_title = clean_filename(title.strip())
    output_path = output_dir / f"{safe_title}.epub"
    epub.write_epub(str(output_path), book)

    print(f"\n✅ EPUB successfully saved to: {output_path}")
