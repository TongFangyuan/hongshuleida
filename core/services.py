from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .analytics import product_metrics, shop_metrics
from .collector import ProductCollector, extract_product_id
from .config import SHANGHAI_TZ
from .database import Database
from .models import CollectionResult, CollectionState, Snapshot
from .notify import CHANNEL_LABELS, mask_webhook, resolve_sender

TZ = ZoneInfo(SHANGHAI_TZ)
SALES_LIMIT = 10_000


class MonitoringService:
    """Application use-cases shared by the Qt client, REST server and MCP server."""
    def __init__(self, database: Database, collector: ProductCollector | None = None):
        self.db = database
        self.collector = collector
        self._lock = asyncio.Lock()
        self.last_run: dict[str, Any] | None = None

    def dashboard(self, keyword: str | None = None) -> list[dict[str, Any]]:
        products = self.db.products()
        histories = self.db.all_snapshots([p["id"] for p in products])
        rows = []
        for product in products:
            if keyword and keyword.lower() not in " ".join(str(product.get(k) or "") for k in ("id", "title", "shop_name")).lower(): continue
            metrics = product_metrics(histories.get(product["id"], []))
            rows.append({**product, **metrics, "status": "异常" if product.get("last_error") else ("待采集" if metrics["cumulative_sold"] is None else "正常")})
        return sorted(rows, key=lambda r: (r["today_sales"] is None, -(r["today_sales"] or 0), r["title"]))

    def shops(self) -> list[dict[str, Any]]:
        products = self.db.products()
        return sorted(shop_metrics(products, self.db.all_snapshots([p["id"] for p in products])), key=lambda r: -(r["today_sales"] or 0))

    def overview(self) -> dict[str, Any]:
        rows, shops = self.dashboard(), self.shops()
        return {"product_count": len(rows), "shop_count": len(shops), "today_sales": sum(r["today_sales"] or 0 for r in rows), "yesterday_sales": sum(r["yesterday_sales"] or 0 for r in rows), "last_hour_sales": sum(r["last_hour_sales"] or 0 for r in rows), "data_at": max((r["data_at"] for r in rows if r["data_at"]), default=None), "usage": self.db.data_usage(), "last_run": self.last_run}

    async def preview_inputs(self, entries: Sequence[str]) -> dict[str, Any]:
        if not self.collector: raise RuntimeError("当前运行环境未启用采集器")
        seen, results = set(), []
        for entry in entries:
            product_id = await self.collector.resolve(entry)
            if not product_id:
                results.append({"input": entry, "state": "invalid"}); continue
            if product_id in seen or self.db.product(product_id):
                results.append({"input": entry, "id": product_id, "state": "duplicate"}); continue
            seen.add(product_id)
            outcome = await self.collector.collect(product_id)
            if outcome.state is CollectionState.SUCCESS and outcome.product and outcome.product.sold > SALES_LIMIT:
                results.append({"input": entry, "id": product_id, "state": "over_limit", "product": outcome.product.__dict__})
            else:
                results.append({"input": entry, "id": product_id, "state": outcome.state, "reason": outcome.reason, "product": outcome.product.__dict__ if outcome.product else None})
        return {"items": results, "summary": {state: sum(1 for x in results if x["state"] == state) for state in ("success", "duplicate", "invalid", "delisted", "over_limit", "temporary_failure")}}

    def confirm_products(self, products: Sequence[dict[str, Any]]) -> int:
        from .models import ProductData
        added = 0
        for raw in products:
            if self.db.product(raw["id"]): continue
            product = ProductData(**raw)
            if product.sold > SALES_LIMIT: continue
            self.db.upsert_product(product)
            self.db.save_snapshot(Snapshot(product.id, datetime.now(TZ), product.sold, product.shop_sold, product.price, product.fans, product.stock_status, product.deliverable))
            added += 1
        return added

    async def collect_products(self, product_ids: Sequence[str] | None = None, formal_time: datetime | None = None) -> dict[str, Any]:
        if not self.collector: raise RuntimeError("当前运行环境未启用采集器")
        if self._lock.locked(): return {"state": "busy", "message": "已有采集任务正在运行"}
        async with self._lock:
            started = datetime.now(TZ)
            products = [p for p in self.db.products() if not product_ids or p["id"] in product_ids]
            failures: list[dict[str, Any]] = []
            restricted = 0
            counts = {"success": 0, "delisted": 0, "failed": 0, "over_limit": 0}
            for product in products:
                result = await self.collector.collect(product["id"])
                action = self._apply_result(product, result, formal_time or started)
                counts[action] = counts.get(action, 0) + 1
                if result.state in (CollectionState.TEMPORARY_FAILURE, CollectionState.RESTRICTED):
                    failures.append({"product": product, "result": result})
                    restricted += result.state is CollectionState.RESTRICTED
                    if restricted >= 3:
                        self.db.log("warning", "collector", "触发 HTTP 461 熔断，剩余商品将在冷却后继续", detail="连续 461 达到阈值")
                        break
                await self.collector.pause()
            # One bounded retry after other products rather than hammering a failed item.
            if restricted < 3 and failures:
                await asyncio.sleep(5)
                for failed in list(failures):
                    result = await self.collector.collect(failed["product"]["id"])
                    action = self._apply_result(failed["product"], result, formal_time or started)
                    if action == "success": failures.remove(failed); counts["success"] += 1
            status = {"state": "cooldown" if restricted >= 3 else "done", "started_at": started.isoformat(), "finished_at": datetime.now(TZ).isoformat(), "counts": counts, "failures": len(failures), "cooldown_until": (datetime.now(TZ) + timedelta(minutes=15)).isoformat() if restricted >= 3 else None}
            self.last_run = status
            await self._notify_if_enabled(formal_time or started, failures)
            return status

    def _apply_result(self, product: dict[str, Any], result: CollectionResult, captured_at: datetime) -> str:
        if result.state is CollectionState.SUCCESS and result.product:
            data = result.product
            if data.sold > SALES_LIMIT:
                self.db.mark_delisted(product["id"], "累计销量超过10000，停止监控")
                return "over_limit"
            self.db.upsert_product(data)
            self.db.save_snapshot(Snapshot(data.id, captured_at, data.sold, data.shop_sold, data.price, data.fans, data.stock_status, data.deliverable))
            self.db.clear_error(data.id)
            self.db.log("success", result.method, "商品采集成功", data.id, data.title)
            return "success"
        if result.state is CollectionState.DELISTED:
            self.db.mark_delisted(product["id"], result.reason or "商品已下架")
            self.db.log("info", result.method, result.reason or "商品已下架", product["id"], product["title"], result.detail)
            return "delisted"
        message = result.reason or "采集失败"
        self.db.mark_error(product["id"], message)
        self.db.log("error", result.method, message, product["id"], product["title"], result.detail)
        return "failed"

    async def _notify_if_enabled(self, at: datetime, failures: list[dict[str, Any]]) -> None:
        settings = self.db.settings()
        resolved = resolve_sender(settings)
        if not resolved: return
        channel, webhook, sender = resolved
        selected = set(filter(None, settings.get("wecom_shop_ids", "").split(",")))
        shops = [s for s in self.shops() if str(s.get("shop_id")) in selected]
        if not shops: return
        ranking = sorted(shops, key=lambda s: -(s["last_hour_sales"] or 0))
        label = at.astimezone(TZ).strftime("%H点店铺销量时报\n%m月%d日 %H:00 - %H:59")
        lines = ["📊 " + label]
        medals = ["🥇", "🥈", "🥉"]
        for index, shop in enumerate(ranking):
            lines.extend(["", f"{medals[index] if index < 3 else str(index + 1) + '.'} {shop['shop_name']}", f"上小时销量：{shop['last_hour_sales'] if shop['last_hour_sales'] is not None else '—'} 单", f"今日总销量：{shop['today_sales'] if shop['today_sales'] is not None else '—'} 单"])
        try:
            await sender(webhook, "\n".join(lines))
            if failures:
                failure_lines = [f"⚠️ {at:%m月%d日 %H:%M} 采集失败 {len(failures)} 个"]
                failure_lines += [f"{f['product']['title']}\n{f['product']['id']}\n{f['result'].method}：{f['result'].reason}" for f in failures]
                await sender(webhook, "\n\n".join(failure_lines))
        except Exception as exc:
            self.db.log("error", channel, f"{CHANNEL_LABELS[channel]}通知发送失败", detail=repr(exc))

    def settings_public(self) -> dict[str, str | None]:
        settings = self.db.settings()
        masked = {key: mask_webhook(settings.get(key)) for key in ("wecom_webhook", "feishu_webhook")}
        return {**{k: v for k, v in settings.items() if k not in masked}, **masked}

    def smart_duplicates(self) -> list[list[dict[str, Any]]]:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in self.dashboard():
            keys = (row.get("shop_id"), " ".join((row.get("title") or "").split()).lower(), row.get("cumulative_sold"), row.get("last_hour_sales"))
            if None in keys or row.get("price") is None: continue
            grouped.setdefault(keys, []).append(row)
        return [sorted(items, key=lambda x: -x["price"]) for items in grouped.values() if len({x["price"] for x in items}) > 1]
