from datetime import datetime
from zoneinfo import ZoneInfo

from core.analytics import high_water_snapshots, product_metrics, sales_between

TZ = ZoneInfo("Asia/Shanghai")


def rows(*entries: tuple[str, int]):
    return [{"captured_at": f"2026-09-{day}T{hour:02}:00:00+08:00", "sold": sold} for day, hour, sold in entries]


def test_high_water_prevents_negative_sales() -> None:
    values = rows(("05", 0, 100), ("05", 1, 95), ("05", 2, 108))
    assert [sold for _, sold, _ in high_water_snapshots(values)] == [100, 100, 108]
    assert sales_between(values, datetime(2026, 9, 5, 0, tzinfo=TZ), datetime(2026, 9, 5, 2, tzinfo=TZ)) == 8


def test_cross_day_metrics_and_missing_baseline() -> None:
    values = rows(("04", 0, 20), ("05", 0, 32), ("05", 1, 35))
    metrics = product_metrics(values, datetime(2026, 9, 5, 1, 30, tzinfo=TZ))
    assert metrics["today_sales"] == 3
    assert metrics["yesterday_sales"] == 12
    assert metrics["last_hour_sales"] == 3
    only_today = rows(("05", 9, 50), ("05", 10, 52))
    assert product_metrics(only_today, datetime(2026, 9, 5, 10, 30, tzinfo=TZ))["today_incomplete"] is True
