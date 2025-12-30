# epub_builder.py
import requests
from ebooklib import epub
from pathlib import Path
from .config import CSS_STYLE, HEADERS
from .utils import clean_filename


def create_epub(
    title: str,
    author: str,
    cover_url: str,
    description: str,
    chapters: list,
    output_dir: Path,
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

    # Cover (Synchronous download is acceptable for one file)
    if cover_url:
        try:
            print("🖼️ Downloading cover...")
            res = requests.get(cover_url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                book.set_cover("cover.jpg", res.content)
        except Exception as e:
            print(f"⚠️ Could not download cover: {e}")

    # Chapters
    epub_chapters = []
    for idx, ch in enumerate(chapters, start=1):
        # Create standardized header
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
