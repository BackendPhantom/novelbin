# NovelBin Scraper & EPUB Builder

A powerful Python tool to scrape novels from [novelbin.com](https://novelbin.com) and compile them into EPUB files for offline reading.

## 🌟 Features

- **Smart Novel Scraping**: Download complete novels or specific chapter ranges from novelbin.com
- **EPUB Generation**: Automatically creates properly formatted EPUB files with metadata and styling
- **Anti-Bot Protection**: Automatic Cloudflare bypass using browser automation
- **Library Management**: Track your favorite novels and check for new chapters
- **Parallel Downloads**: Fast concurrent chapter downloading with progress tracking
- **Smart Caching**: Caches chapter content to avoid redundant downloads
- **Cookie Persistence**: Saves authentication cookies for seamless access
- **Update Checking**: Automatically detects new chapters for tracked novels
- **Termux Support**: Works on Android devices via Termux

## 📋 Requirements

- Python 3.8+
- Chrome/Chromium browser (for Cloudflare bypass)

## 🚀 Installation

1. Clone the repository:

```bash
git clone https://github.com/BackendPhantom/novelbin.git
cd novelbin
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

## 📖 Usage

### Basic Usage

Run the program:

```bash
python -m novelbin.main
```

### Quick Search Mode

Search directly from command line:

```bash
python -m novelbin.main -q "novel name"
# or
python -m novelbin.main --query "novel name"
```

### Main Menu Options

1. **🔍 Search & Download**

   - Search for novels by name
   - View search results and select a novel
   - Choose specific chapters or download all
   - Option to add novel to library for tracking

2. **🔄 Check for Updates**

   - Automatically checks all novels in your library
   - Detects new chapters since last download
   - Quick download option for new chapters only

3. **❌ Exit**
   - Exit the application

### Chapter Selection

When downloading, you can select chapters using various formats:

- `1-10` - Download chapters 1 through 10
- `1,5,10` - Download chapters 1, 5, and 10
- `1-5,10-15` - Download chapters 1-5 and 10-15
- Press Enter - Download all chapters

## 🗂️ Project Structure

```
novelbin/
├── config.py           # Configuration settings and constants
├── main.py            # Main application entry point
├── scraper.py         # Novel scraping and web requests
├── solver.py          # Cloudflare bypass automation
├── epub_builder.py    # EPUB file creation
├── library.py         # Library management for tracking
└── utils.py           # Helper utilities
```

## ⚙️ Configuration

Default settings in `config.py`:

- **Output Directory**: `~/Documents/Novels` (Desktop) or `/storage/emulated/0/Documents/Novels/New` (Termux)
- **Max Concurrent Requests**: 3
- **Request Timeout**: 30 seconds
- **Max Retries**: 3

## 🔧 How It Works

1. **Search**: Queries novelbin.com for matching novels
2. **Metadata Extraction**: Scrapes novel title, author, cover image, description, and chapter list
3. **Anti-Bot Bypass**: Automatically handles Cloudflare protection using browser automation
4. **Chapter Download**: Downloads chapter content with parallel processing
5. **EPUB Building**: Creates a properly formatted EPUB file with:
   - Cover image
   - Metadata (title, author, description)
   - Table of contents
   - Styled chapter content
6. **Library Tracking**: Saves novel information for future update checks

## 🛡️ Features in Detail

### Automatic Cloudflare Bypass

The scraper automatically detects when it encounters Cloudflare protection and launches a browser session to obtain valid cookies. These cookies are saved and reused for subsequent requests.

### Smart Caching

Chapter content is cached locally to avoid redundant downloads if you need to rebuild an EPUB or re-select chapters.

### Cookie Persistence

Authentication cookies are saved to disk and automatically loaded on startup, reducing the need for frequent browser automation.

### Error Handling

- Automatic retries on failed requests
- Progress tracking with real-time updates
- Detailed error reporting for failed chapters

## 📁 Output Files

- **EPUB files**: Saved to the configured output directory
- **Library**: `library.json` stores tracked novels
- **Cookies**: `cookies.json` stores authentication data
- **Cache**: `.cache/` directory stores downloaded chapter content

## 🐛 Troubleshooting

### 403 Forbidden Errors

The scraper automatically handles Cloudflare protection. If issues persist:

- Ensure Chrome/Chromium is installed
- Check your internet connection
- Try clearing the cache and cookies files

### Missing Chapters

If some chapters fail to download:

- Check the website availability
- Verify the chapter URLs are valid
- Retry the download

## 📝 License

This project is for educational purposes only. Please respect the original content creators and the website's terms of service.

## ⚠️ Disclaimer

This tool is intended for personal use only. Users are responsible for complying with novelbin.com's terms of service and copyright laws. The developers are not responsible for any misuse of this software.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## 📧 Support

If you encounter any issues or have questions, please open an issue on GitHub.
