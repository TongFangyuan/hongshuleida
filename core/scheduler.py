from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import SHANGHAI_TZ

TZ = ZoneInfo(SHANGHAI_TZ)


def next_boundary(now: datetime, period_minutes: int) -> datetime:
    if not 1 <= period_minutes <= 1440: raise ValueError("采集周期必须在 1 到 1440 分钟之间")
    local = now.astimezone(TZ).replace(second=0, microsecond=0)
    day_start = local.replace(hour=0, minute=0)
    elapsed = int((local - day_start).total_seconds() // 60)
    return day_start + timedelta(minutes=(elapsed // period_minutes + 1) * period_minutes)


class BoundaryScheduler:
    def __init__(self, job: Callable[[datetime], Awaitable[object]], period_minutes: int = 60):
        self.job, self.period_minutes, self.running, self._task = job, period_minutes, False, None
        self.next_run_at: datetime | None = None
        self._wake: asyncio.Event | None = None
        self._jobs: set[asyncio.Task[None]] = set()

    async def _sleep(self, seconds: float) -> bool:
        if seconds <= 0: return False
        if self._wake is None: self._wake = asyncio.Event()
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_job(self, boundary: datetime) -> None:
        try:
            await self.job(boundary)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _run(self) -> None:
        try:
            while self.running:
                boundary = next_boundary(datetime.now(TZ), self.period_minutes)
                self.next_run_at = boundary
                woken = await self._sleep(max(0.0, (boundary - datetime.now(TZ)).total_seconds()))
                self.next_run_at = None
                if not self.running or woken: continue
                task = asyncio.create_task(self._run_job(boundary))
                self._jobs.add(task); task.add_done_callback(self._jobs.discard)
        finally:
            self.next_run_at = None

    def start(self) -> None:
        if not self.running:
            self.running = True; self._task = asyncio.create_task(self._run())

    def reconfigure(self, period_minutes: int) -> None:
        if not 1 <= period_minutes <= 1440: raise ValueError("采集周期必须在 1 到 1440 分钟之间")
        self.period_minutes = period_minutes
        if self._wake: self._wake.set()

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError): await self._task
        self._task = None
