import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import core.scheduler as scheduler_module
from core.scheduler import BoundaryScheduler, next_boundary

tz = ZoneInfo("Asia/Shanghai")


def test_next_boundary_uses_natural_time_boundary() -> None:
    assert next_boundary(datetime(2026, 9, 6, 9, 35, tzinfo=tz), 60) == datetime(2026, 9, 6, 10, 0, tzinfo=tz)
    assert next_boundary(datetime(2026, 9, 6, 9, 35, tzinfo=tz), 15) == datetime(2026, 9, 6, 9, 45, tzinfo=tz)


def test_reconfigure_wakes_timer_and_recomputes_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fired: list[datetime] = []
        seen: list[int] = []
        fifteen_calls = [0]

        def fake_boundary(now: datetime, period_minutes: int) -> datetime:
            seen.append(period_minutes)
            quick = False
            if period_minutes == 15:
                fifteen_calls[0] += 1
                quick = fifteen_calls[0] == 1
            return now + timedelta(seconds=0.05 if quick else 5)

        monkeypatch.setattr(scheduler_module, "next_boundary", fake_boundary)

        async def job(boundary: datetime) -> None:
            fired.append(boundary)

        scheduler = BoundaryScheduler(job, 60)
        scheduler.start()
        await asyncio.sleep(0.02)
        assert scheduler.next_run_at is not None and not fired
        scheduler.reconfigure(15)
        await asyncio.sleep(0.15)
        assert scheduler.running and 60 in seen and 15 in seen and len(fired) == 1
        await scheduler.stop()
        assert not scheduler.running and scheduler.next_run_at is None

    asyncio.run(scenario())


def test_stop_does_not_cancel_in_flight_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def job(boundary: datetime) -> None:
            started.set()
            await release.wait()
            completed.set()

        calls = [0]

        def fake_boundary(now: datetime, period_minutes: int) -> datetime:
            calls[0] += 1
            return now + timedelta(seconds=0.01 if calls[0] == 1 else 30)

        monkeypatch.setattr(scheduler_module, "next_boundary", fake_boundary)
        scheduler = BoundaryScheduler(job, 1)
        scheduler.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        await scheduler.stop()
        assert not scheduler.running and scheduler.next_run_at is None and calls[0] >= 2
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)

    asyncio.run(scenario())
