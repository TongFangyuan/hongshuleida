"""Read-only stdio MCP server for the same Red Potato Radar SQLite database."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from core.analytics import daily_sales, hourly_sales
from core.config import SHANGHAI_TZ, app_paths
from core.database import Database
from core.services import MonitoringService

TZ = ZoneInfo(SHANGHAI_TZ)
mcp = FastMCP("xhs-sales-monitor", instructions="红薯雷达只读数据服务。所有销量来自可信累计已售快照差值；不得将结果用于写入或删除数据。")


def service() -> MonitoringService:
    return MonitoringService(Database(app_paths().database))


@mcp.tool()
def get_overview() -> dict:
    """Return monitored product/shop counts, sales totals, and snapshot cutoff time."""
    return {**service().overview(), "caliber": "销量是累计已售快照差值；缺少基线时为 null，不伪造 0", "timezone": SHANGHAI_TZ}


@mcp.tool()
def list_shops() -> dict:
    """List monitored shops with product count, today and last completed-hour sales."""
    return {"items": service().shops(), "caliber": "店铺销量为该店铺已监控商品销量之和", "data_time": datetime.now(TZ).isoformat()}


@mcp.tool()
def list_products(shop_id: str | None = None, keyword: str | None = None, status: Literal["active", "delisted", "all"] = "active") -> dict:
    """List products. Filter by shop ID, title/shop/ID keyword, and monitoring state."""
    svc = service(); rows = svc.dashboard(keyword)
    if shop_id: rows = [row for row in rows if row.get("shop_id") == shop_id]
    if status == "delisted": rows = [row for row in svc.db.products(True) if not row["active"]]
    elif status == "all": rows += [row for row in svc.db.products(True) if not row["active"]]
    return {"items": rows, "caliber": "today/yesterday/last_hour 均为快照高水位差", "data_time": datetime.now(TZ).isoformat()}


@mcp.tool()
def search_products(query: str) -> dict:
    """Search product title, shop name, and product ID."""
    return list_products(keyword=query)


@mcp.tool()
def get_product_sales(product_id: str, day: str | None = None, days: int = 7) -> dict:
    """Get hourly sales for a YYYY-MM-DD day, or daily sales for recent N days."""
    svc = service()
    if not svc.db.product(product_id): return {"error": "商品不存在"}
    rows = svc.db.snapshots(product_id)
    payload = {"hourly": hourly_sales(rows, datetime.fromisoformat(day).date())} if day else {"daily": daily_sales(rows, min(max(days, 1), 365))}
    return {"product_id": product_id, **payload, "caliber": "相邻或边界快照的累计已售高水位差", "data_time": datetime.now(TZ).isoformat()}


@mcp.tool()
def get_shop_sales(shop_id: str, days: int = 7) -> dict:
    """Get recent daily aggregate sales for all monitored products of a shop."""
    svc = service(); products = [p for p in svc.db.products() if p.get("shop_id") == shop_id]
    if not products: return {"error": "店铺不存在"}
    histories = svc.db.all_snapshots([p["id"] for p in products])
    buckets: dict[str, int] = {}
    for product in products:
        for point in daily_sales(histories[product["id"]], min(max(days, 1), 365)):
            buckets[point["date"]] = buckets.get(point["date"], 0) + (point["sales"] or 0)
    return {"shop_id": shop_id, "daily": [{"date": date, "sales": sales} for date, sales in sorted(buckets.items())], "caliber": "店铺内已监控商品日销量求和；缺失单品基线不计入", "data_time": datetime.now(TZ).isoformat()}


@mcp.tool()
def get_product_ranking(by: Literal["today", "yesterday", "last_hour", "cumulative_sold", "estimated_revenue"] = "today", limit: int = 50) -> dict:
    """Rank products by sales or estimated revenue. Revenue is explicitly an estimate."""
    field = {"today": "today_sales", "yesterday": "yesterday_sales", "last_hour": "last_hour_sales", "cumulative_sold": "cumulative_sold"}.get(by, "today_sales")
    rows = service().dashboard()
    if by == "estimated_revenue":
        for row in rows: row["estimated_revenue"] = (row["last_hour_sales"] or 0) * (row.get("price") or 0)
        field = "estimated_revenue"
    return {"items": sorted(rows, key=lambda row: -(row.get(field) or -1))[:min(max(limit, 1), 200)], "caliber": "estimated_revenue 是销量增量×当时价格的模糊试算，不代表真实成交额", "data_time": datetime.now(TZ).isoformat()}


@mcp.tool()
def get_shop_ranking(by: Literal["today", "yesterday", "last_hour"] = "today", limit: int = 50) -> dict:
    """Rank monitored shops by aggregated sales."""
    field = {"today": "today_sales", "yesterday": "yesterday_sales", "last_hour": "last_hour_sales"}[by]
    return {"items": sorted(service().shops(), key=lambda row: -(row.get(field) or -1))[:min(max(limit, 1), 200)], "caliber": "店铺内已监控商品销量之和", "data_time": datetime.now(TZ).isoformat()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
