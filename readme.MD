# NovelBin

NovelBin is a Python CLI for scraping novels from novelarrow.com, downloading selected chapters, and packaging them into EPUB files for offline reading.

## What the current codebase does

- Searches for novels and resolves direct URLs
- Downloads chapter content with concurrency, retries, and adaptive pacing
- Builds EPUB files with a cover image, metadata, styled chapter content, and a chapter list
- Tracks novels in a local library and checks for new chapters later
- Supports background job monitoring and batch URL processing
- Can refresh metadata and cover art for an existing EPUB
- Can save raw HTML samples for debugging or inspection

## Requirements

- Python 3.8+
- The runtime dependencies used by the project, including:
  - curl_cffi
  - beautifulsoup4
  - aiofiles
- Optional: httpx for the transport fallback path

## Installation

From the parent directory that contains the novelbin package:

```bash
python -m pip install curl-cffi beautifulsoup4 aiofiles
python -m novelbin.main
```

## Running the CLI

### Quick search

```bash
python -m novelbin.main -q "novel name"
# or
python -m novelbin.main --query "novel name"
```

### Download one or more novels

```bash
python -m novelbin.main --url https://novelarrow.com/b/novel-one
```

```bash
python -m novelbin.main --urls https://novelarrow.com/b/novel-one https://novelarrow.com/b/novel-two https://novelarrow.com/b/novel-three
```

### Save raw HTML samples

```bash
python -m novelbin.main --save-html "novel name"
```

### Refresh cover and metadata for an existing EPUB

```bash
python -m novelbin.main --refresh-cover "novel name" --epub-path /path/to/book.epub
```

## Interactive commands

When the app starts, it enters an interactive prompt with commands such as:

- search <query> - search for a novel and choose a result
- download <name-or-url>[,...] - download one or more novels
- update - check the library for new chapters
- meta <url|name|path> - refresh metadata and cover art for an existing EPUB
- status - show active jobs
- watch - monitor job progress
- stopwatch - stop the monitor
- cancel <job#|url> - cancel a queued or running job
- library - list tracked novels
- exit - quit the CLI

## Chapter selection

When prompted for chapters, you can use any of these formats:

- 1-10: download chapters 1 through 10
- 1,5,10: download specific chapters
- 1-5,10-15: download multiple ranges
- Press Enter: download all chapters

## Output locations

By default, EPUBs and supporting files are written to:

- Desktop: ~/Documents/Novels
- Termux: /storage/emulated/0/Documents/Novels/New

The tool also creates:

- .cache/ for cached chapter content
- library.json for tracked novels
- cookies.json for saved request state
- .browser_profile/ for browser-related data

## Notes

The scraper uses adaptive request pacing and retry handling, and it tries to recover from Cloudflare-style challenges by reusing saved cookies and request state. Batch downloads are handled through a task manager so multiple novels can be processed without blocking the interactive CLI.

## License and disclaimer

This project is intended for personal use only. Respect the original content creators, the website’s terms of service, and local copyright laws.
