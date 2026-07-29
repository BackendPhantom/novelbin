# main.py
import logging
import sys


def setup_logging():
    # 1. Grab the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture everything internally

    # 2. Console Handler (What the user sees in the terminal)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)  # Only show INFO, WARNING, ERROR, CRITICAL
    console_format = logging.Formatter("%(levelname)s: %(message)s")
    console_handler.setFormatter(console_format)

    # 3. File Handler (What you look at for debugging)
    file_handler = logging.FileHandler("scraper_debug.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # Save EVERYTHING, including tracebacks
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_format)

    # 4. Attach handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


# Call this immediately when the script starts
setup_logging()
