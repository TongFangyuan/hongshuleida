from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from core.analytics import daily_sales, hourly_sales
from core.collector import ProductCollector
from core.config import SHANGHAI_TZ, app_paths
from core.database import Database
from core.scheduler import BoundaryScheduler
from core.services import MonitoringService
from core.feishu import valid_webhook as valid_feishu_webhook
from core.notify import is_masked, notification_error, resolve_sender, test_message
from core.wecom import valid_webhook as valid_wecom_webhook

TZ = ZoneInfo(SHANGHAI_TZ)
WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


class AddRequest(BaseModel):
    entries: list[str] = Field(min_length=1, max_length=300)


class ConfirmRequest(BaseModel):
    products: list[dict] = Field(default_factory=list)


class CollectRequest(BaseModel):
    product_ids: list[str] | None = None


class SettingsRequest(BaseModel):
    values: dict[str, str | None]

    @field_validator("values")
    @classmethod
    def validate_settings(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        for key, validator, message in (("wecom_webhook", valid_wecom_webhook, "企业微信 Webhook 格式无效"), ("feishu_webhook", valid_feishu_webhook, "飞书 Webhook 格式无效")):
            raw = value.get(key)
            if raw and not is_masked(raw) and not validator(raw): raise ValueError(message)
        if "notify_channel" in value and value["notify_channel"] not in ("wecom", "feishu"):
            raise ValueError("notify_channel 必须是 wecom 或 feishu")
        for key in ("interval_min_seconds", "interval_max_seconds"):
            if value.get(key) is not None and not 0.1 <= float(value[key]) <= 60: raise ValueError(f"{key} 必须在 0.1 到 60 之间")
        if value.get("collection_period_minutes") is not None and not 1 <= int(value["collection_period_minutes"]) <= 1440:
            raise ValueError("collection_period_minutes 必须在 1 到 1440 之间")
        return value


class CleanRequest(BaseModel):
    snapshot_days: int = Field(default=90, ge=1, le=3650)
    log_days: int = Field(default=30, ge=1, le=3650)


def require_api_token() -> None:
    """When binding away from localhost, deployment must set RED_POTATO_RADAR_API_TOKEN."""
    # Router-level authentication is intentionally configured at startup below.


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("RED_POTATO_RADAR_PUBLIC", "false").lower() == "true" and not os.environ.get("RED_POTATO_RADAR_API_TOKEN"):
        raise RuntimeError("公网模式必须设置 RED_POTATO_RADAR_API_TOKEN，并在 HTTPS 反向代理后运行")
    paths = app_paths()
    db = Database(paths.database)
    settings = db.settings()
    collector = ProductCollector(paths.browser_profile, float(settings.get("interval_min_seconds", "0.1")), float(settings.get("interval_max_seconds", "0.3")), os.environ.get("HTTPS_PROXY"))
    service = MonitoringService(db, collector)
    scheduler = BoundaryScheduler(lambda boundary: service.collect_products(formal_time=boundary), int(settings.get("collection_period_minutes", "60")))
    app.state.db, app.state.service, app.state.collector, app.state.scheduler = db, service, collector, scheduler
    if settings.get("monitoring_enabled") == "true": scheduler.start()
    yield
    await scheduler.stop()
    await collector.close()


app = FastAPI(title="红薯雷达 API", version="1.0.0", lifespan=lifespan)
allowed_origins = [origin for origin in os.environ.get("RED_POTATO_RADAR_ALLOWED_ORIGINS", "").split(",") if origin]
if allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def bearer_authentication(request: Request, call_next):
    """Bearer authentication is opt-in locally and mandatory for declared public use."""
    token = os.environ.get("RED_POTATO_RADAR_API_TOKEN")
    if request.url.path.startswith("/api") and request.url.path != "/api/health" and token:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"detail": "未授权"}, status_code=401)
    return await call_next(request)


def service() -> MonitoringService:
    return app.state.service


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "timezone": SHANGHAI_TZ}


@app.get("/api/overview")
async def overview() -> dict:
    return service().overview()


@app.get("/api/products")
async def products(keyword: str | None = None, status: Literal["active", "delisted", "all"] = "active") -> list[dict]:
    rows = service().dashboard(keyword)
    return rows if status == "active" else ([p for p in service().db.products(True) if not p["active"]] if status == "delisted" else rows + [p for p in service().db.products(True) if not p["active"]])


@app.get("/api/products/{product_id}")
async def product(product_id: str) -> dict:
    item = service().db.product(product_id)
    if not item: raise HTTPException(404, "商品不存在")
    dashboard_rows = service().dashboard(product_id)
    return {**item, **(dashboard_rows[0] if dashboard_rows else {}), "snapshots": service().db.snapshots(product_id)}


@app.post("/api/products/preview")
async def preview(request: AddRequest) -> dict:
    return await service().preview_inputs([entry.strip() for entry in request.entries if entry.strip()])


@app.post("/api/products/confirm")
async def confirm(request: ConfirmRequest) -> dict:
    return {"added": service().confirm_products(request.products)}


@app.delete("/api/products")
async def delete_products(product_ids: Annotated[list[str], Query()]) -> dict:
    return {"deleted": service().db.delete_products(product_ids)}


@app.post("/api/collect")
async def collect(request: CollectRequest, background: BackgroundTasks) -> dict:
    if service()._lock.locked(): return {"state": "busy"}
    background.add_task(service().collect_products, request.product_ids)
    return {"state": "queued"}


@app.get("/api/collect/status")
async def collect_status() -> dict:
    scheduler = getattr(app.state, "scheduler", None)
    return {"running": service()._lock.locked(), "last_run": service().last_run,
            "scheduled": scheduler.running if scheduler else False,
            "period_minutes": scheduler.period_minutes if scheduler else None,
            "next_run_at": scheduler.next_run_at.isoformat() if scheduler and scheduler.next_run_at else None}


@app.get("/api/shops")
async def shops() -> list[dict]:
    return service().shops()


@app.get("/api/shops/{shop_id}/sales")
async def shop_sales(shop_id: str, days: int = 7) -> dict:
    products = [p for p in service().db.products() if p.get("shop_id") == shop_id]
    if not products: raise HTTPException(404, "店铺不存在")
    histories = service().db.all_snapshots([p["id"] for p in products])
    # Sum matching buckets; no missing value is falsely converted to zero.
    days = min(max(days, 1), 365)
    return {"shop": service().shops(), "daily": [{"date": (datetime.now(TZ).date() - timedelta(days=i)).isoformat(), "sales": sum(x["sales"] or 0 for p in products for x in daily_sales(histories[p["id"]], days) if x["date"] == (datetime.now(TZ).date() - timedelta(days=i)).isoformat())} for i in reversed(range(days))]}


@app.get("/api/products/{product_id}/sales")
async def product_sales(product_id: str, day: str | None = None, days: int | None = None) -> dict:
    if not service().db.product(product_id): raise HTTPException(404, "商品不存在")
    rows = service().db.snapshots(product_id)
    if day: return {"hourly": hourly_sales(rows, datetime.fromisoformat(day).date()), "data_time": datetime.now(TZ).isoformat()}
    return {"daily": daily_sales(rows, min(max(days or 7, 1), 365)), "data_time": datetime.now(TZ).isoformat()}


@app.get("/api/settings")
async def get_settings() -> dict:
    return service().settings_public()


@app.put("/api/settings")
async def set_settings(request: SettingsRequest) -> dict:
    db = service().db
    updates = {key: value for key, value in request.values.items() if value is not None and not (key in ("wecom_webhook", "feishu_webhook") and is_masked(value))}
    # A masked webhook from a GET response is never persisted back as the secret.
    merged = {**db.settings(), **updates}
    error = notification_error(merged)
    if error: raise HTTPException(422, error)
    for key, value in updates.items(): db.set_setting(key, value)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        scheduler.reconfigure(int(merged.get("collection_period_minutes") or 60))
        if merged.get("monitoring_enabled") == "true": scheduler.start()
        else: await scheduler.stop()
    return service().settings_public()


@app.post("/api/settings/test-notify")
async def test_notify() -> dict:
    resolved = resolve_sender(service().db.settings())
    if not resolved: raise HTTPException(400, "尚未启用通知或所选渠道未配置 Webhook")
    channel, webhook, sender = resolved
    try:
        await sender(webhook, test_message(channel))
    except Exception as exc:
        service().db.log("error", channel, "测试通知失败", detail=repr(exc))
        raise HTTPException(502, "发送失败，详情已写入运行日志") from exc
    return {"ok": True}


@app.get("/api/logs")
async def logs(level: str | None = None, limit: int = 500) -> list[dict]:
    return service().db.logs(level, limit)


@app.delete("/api/logs")
async def clear_logs() -> dict:
    return {"deleted": service().db.clear_logs()}


@app.post("/api/cleanup")
async def cleanup(request: CleanRequest) -> dict:
    now = datetime.now(TZ)
    return service().db.cleanup((now - timedelta(days=request.snapshot_days)).isoformat(), (now - timedelta(days=request.log_days)).isoformat())


@app.get("/api/duplicates")
async def duplicates() -> list[list[dict]]:
    return service().smart_duplicates()


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        target = WEB_DIST / path
        return FileResponse(target) if path and target.is_file() else FileResponse(WEB_DIST / "index.html")
