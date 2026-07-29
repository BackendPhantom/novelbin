# main.py
import asyncio

from .cli import run_cli
from .logger import setup_logging


def start():
    setup_logging()
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")


if __name__ == "__main__":
    start()
