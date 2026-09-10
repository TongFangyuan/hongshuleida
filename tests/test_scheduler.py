from datetime import datetime
from zoneinfo import ZoneInfo

from core.scheduler import next_boundary


def test_next_boundary_uses_natural_time_boundary() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    assert next_boundary(datetime(2026, 9, 6, 9, 35, tzinfo=tz), 60) == datetime(2026, 9, 6, 10, 0, tzinfo=tz)
    assert next_boundary(datetime(2026, 9, 6, 9, 35, tzinfo=tz), 15) == datetime(2026, 9, 6, 9, 45, tzinfo=tz)
