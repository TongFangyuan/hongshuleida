from __future__ import annotations

import asyncio
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

    async def _run(self) -> None:
        while self.running:
            boundary = next_boundary(datetime.now(TZ), self.period_minutes)
            await asyncio.sleep(max(0, (boundary - datetime.now(TZ)).total_seconds()))
            if self.running: await self.job(boundary)

    def start(self) -> None:
        if not self.running:
            self.running = True; self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._task: self._task.cancel()
        self._task = None
