from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import SHANGHAI_TZ

TZ = ZoneInfo(SHANGHAI_TZ)


def parse_time(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def high_water_snapshots(rows: Iterable[dict[str, Any]]) -> list[tuple[datetime, int, dict[str, Any]]]:
    """Monotonic cumulative sales: a lower later read cannot produce negative sales."""
    high, result = -1, []
    for row in sorted(rows, key=lambda item: parse_time(item["captured_at"])):
        sold = int(row["sold"])
        high = max(high, sold)
        result.append((parse_time(row["captured_at"]), high, row))
    return result


def high_water_at(rows: Iterable[dict[str, Any]], boundary: datetime) -> int | None:
    candidates = [(when, sold) for when, sold, _ in high_water_snapshots(rows) if when <= boundary]
    return candidates[-1][1] if candidates else None


def sales_between(rows: Iterable[dict[str, Any]], start: datetime, end: datetime) -> int | None:
    baseline, finish = high_water_at(rows, start), high_water_at(rows, end)
    return None if baseline is None or finish is None else max(0, finish - baseline)


def product_metrics(rows: Iterable[dict[str, Any]], now: datetime | None = None) -> dict[str, int | None | str]:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    today_start = datetime.combine(now.date(), time.min, tzinfo=TZ)
    yesterday_start = today_start - timedelta(days=1)
    complete_hour = now.replace(minute=0, second=0, microsecond=0)
    prev_hour = complete_hour - timedelta(hours=1)
    waters = high_water_snapshots(rows)
    current = waters[-1][1] if waters else None
    today_base = high_water_at(rows, today_start)
    # First real snapshot of today is a truthful but explicitly incomplete baseline.
    incomplete = False
    if today_base is None:
        today_rows = [item for item in waters if item[0] >= today_start]
        today_base = today_rows[0][1] if today_rows else None
        incomplete = today_base is not None
    return {
        "cumulative_sold": current,
        "today_sales": None if current is None or today_base is None else max(0, current - today_base),
        "yesterday_sales": sales_between(rows, yesterday_start, today_start),
        "last_hour_sales": sales_between(rows, prev_hour, complete_hour),
        "today_incomplete": incomplete,
        "data_at": waters[-1][0].isoformat() if waters else None,
    }


def hourly_sales(rows: Iterable[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    start = datetime.combine(day, time.min, tzinfo=TZ)
    return [{"time": (start + timedelta(hours=h)).isoformat(), "sales": sales_between(rows, start + timedelta(hours=h), start + timedelta(hours=h + 1))} for h in range(24)]


def daily_sales(rows: Iterable[dict[str, Any]], days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    today = (now or datetime.now(TZ)).astimezone(TZ).date()
    return [{"date": (today - timedelta(days=offset)).isoformat(), "sales": sales_between(rows, datetime.combine(today - timedelta(days=offset), time.min, tzinfo=TZ), datetime.combine(today - timedelta(days=offset - 1), time.min, tzinfo=TZ))} for offset in reversed(range(days))]


def shop_metrics(products: list[dict[str, Any]], snapshots: dict[str, list[dict[str, Any]]], now: datetime | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        grouped[product.get("shop_id") or product.get("shop_name") or "unknown"].append(product)
    result = []
    for shop_key, members in grouped.items():
        metrics = [product_metrics(snapshots.get(p["id"], []), now) for p in members]
        sum_field = lambda field: sum(int(m[field] or 0) for m in metrics) if any(m[field] is not None for m in metrics) else None
        result.append({"shop_id": members[0].get("shop_id"), "shop_name": members[0].get("shop_name") or "未命名店铺", "product_count": len(members), "today_sales": sum_field("today_sales"), "yesterday_sales": sum_field("yesterday_sales"), "last_hour_sales": sum_field("last_hour_sales"), "cumulative_sold": sum_field("cumulative_sold")})
    return result
