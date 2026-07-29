from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class ScrapeJob:
    url: str
    status: str = "queued"
    title: str = ""
    error: str = ""
    message: str = ""  # Progress/detail message
    output_path: Optional[Path] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        return self.url.rsplit("/", 1)[-1] or self.url


class ScrapeTaskManager:
    def __init__(self, auto_print: bool = True):
        self.jobs: List[ScrapeJob] = []
        self._locks: Dict[str, asyncio.Lock] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._background_runs: List[asyncio.Task] = []
        self.auto_print = auto_print

    def create_job(self, url: str) -> ScrapeJob:
        job = ScrapeJob(url=url)
        self.jobs.append(job)
        self.set_status(job, "queued")
        return job

    def set_status(self, job: ScrapeJob, status: str, message: str = "") -> None:
        job.status = status
        job.message = message  # Store the message for status display
        job.updated_at = time.time()
        if not self.auto_print:
            return
        prefix = f"[{status}] {job.label}"
        # Ensure status messages start on their own line so they don't clobber prompts
        if message:
            print(f"\n{prefix}: {message}")
        else:
            print(f"\n{prefix}")

    async def run(
        self,
        urls: List[str],
        runner: Callable[[ScrapeJob, "ScrapeTaskManager"], Awaitable[Any]],
        concurrency: int = 3,
    ):
        sem = asyncio.Semaphore(concurrency)

        # Create every job up front so `status` can list all queued items immediately.
        jobs_by_url: Dict[str, ScrapeJob] = {}
        for url in urls:
            jobs_by_url[url] = self.create_job(url)

        async def _worker(u: str):
            async with sem:
                return await self._run_single(jobs_by_url[u], runner)

        tasks = []
        for url in urls:
            t = asyncio.create_task(_worker(url))
            # keep track so callers can cancel by URL
            self._running_tasks[url] = t

            def _on_done(_t, u=url):
                try:
                    # cleanup mapping when finished
                    self._running_tasks.pop(u, None)
                except Exception:
                    pass

            t.add_done_callback(_on_done)
            tasks.append(t)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return self.jobs, results

    async def _run_single(
        self,
        job: ScrapeJob,
        runner: Callable[[ScrapeJob, "ScrapeTaskManager"], Awaitable[Any]],
    ):
        try:
            return await runner(job, self)
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            print(f"[failed] {job.label}: {exc}")
            return None

    def list_jobs(self) -> List[ScrapeJob]:
        return list(self.jobs)

    async def start_background(
        self,
        urls: List[str],
        runner: Callable[[ScrapeJob, "ScrapeTaskManager"], Awaitable[Any]],
        concurrency: int = 2,
    ) -> asyncio.Task:
        """Start a background run and return the asyncio.Task handle."""
        bg = asyncio.create_task(self.run(urls, runner, concurrency))
        self._background_runs.append(bg)
        return bg

    def cancel_url(self, url: str) -> bool:
        """Attempt to cancel an in-flight task for the given URL. Returns True if cancelled."""
        t = self._running_tasks.get(url)
        if not t:
            # find job and mark cancelled if queued
            for job in self.jobs:
                if job.url == url and job.status == "queued":
                    job.status = "cancelled"
                    job.updated_at = time.time()
                    self.set_status(job, "cancelled")
                    return True
            return False
        t.cancel()
        # mark job cancelled
        for job in self.jobs:
            if job.url == url:
                job.status = "cancelled"
                job.updated_at = time.time()
                self.set_status(job, "cancelled")
                break
        return True

    async def run_exclusive(
        self, key: str, func: Callable[..., T], *args, **kwargs
    ) -> T:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # If the provided function is an async coroutine, await it directly in the loop
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)

            # If it's a standard synchronous function, push it to a background thread
            return await asyncio.to_thread(func, *args, **kwargs)
