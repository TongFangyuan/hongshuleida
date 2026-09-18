from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import shutil
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
from core.backup import BackupScheduler, BackupService
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
IMPORT_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
DEFAULT_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024


class AddRequest(BaseModel):
    entries: list[str] = Field(min_length=1, max_length=300)


class ConfirmRequest(BaseModel):
    products: list[dict] = Field(default_factory=list)


class CollectRequest(BaseModel):
    product_ids: list[str] | None = None


class NotificationShopsRequest(BaseModel):
    shop_ids: list[str] = Field(default_factory=list, max_length=1000)


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
        if value.get("backup_enabled") is not None and value["backup_enabled"] not in ("true", "false"):
            raise ValueError("backup_enabled 必须是 true 或 false")
        if value.get("backup_schedule") is not None and value["backup_schedule"] not in ("daily", "weekly", "startup"):
            raise ValueError("backup_schedule 必须是 daily、weekly 或 startup")
        for key in ("backup_daily_time", "backup_weekly_time"):
            if value.get(key) is not None:
                try:
                    datetime.strptime(value[key] or "", "%H:%M")
                except ValueError as exc:
                    raise ValueError(f"{key} 必须是 HH:MM") from exc
        if value.get("backup_weekday") is not None and not 0 <= int(value["backup_weekday"] or 0) <= 6:
            raise ValueError("backup_weekday 必须在 0 到 6 之间")
        if value.get("backup_keep_count") is not None and not 1 <= int(value["backup_keep_count"] or 0) <= 100:
            raise ValueError("backup_keep_count 必须在 1 到 100 之间")
        return value


class CleanRequest(BaseModel):
    snapshot_days: int = Field(default=90, ge=1, le=3650)
    log_days: int = Field(default=30, ge=1, le=3650)


class RestoreRequest(BaseModel):
    confirmation_token: str = Field(min_length=16, max_length=256)


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
    write_lock = asyncio.Lock()

    async def scheduled_collection(boundary: datetime) -> dict:
        async with write_lock:
            if app.state.maintenance:
                return {"state": "maintenance"}
            return await service.collect_products(formal_time=boundary)

    scheduler = BoundaryScheduler(scheduled_collection, int(settings.get("collection_period_minutes", "60")))
    maintenance_lock = asyncio.Lock()
    backup_service = BackupService(db, paths.backups)

    async def automatic_backup(trigger_type: str) -> dict:
        async with maintenance_lock:
            return await run_blocking(backup_service.create, trigger_type)

    backup_scheduler = BackupScheduler(automatic_backup, db.settings)
    app.state.paths, app.state.db, app.state.service, app.state.collector, app.state.scheduler = paths, db, service, collector, scheduler
    app.state.backup_service, app.state.backup_scheduler = backup_service, backup_scheduler
    app.state.maintenance_lock, app.state.import_lock, app.state.restore_lock, app.state.write_lock, app.state.maintenance, app.state.maintenance_generation, app.state.restore_tokens = maintenance_lock, asyncio.Lock(), asyncio.Lock(), write_lock, False, 0, {}
    if settings.get("monitoring_enabled") == "true": scheduler.start()
    if settings.get("backup_enabled") == "true": backup_scheduler.start()
    yield
    await backup_scheduler.stop()
    await scheduler.stop()
    await collector.close()


app = FastAPI(title="红薯雷达 API", version="1.0.0", lifespan=lifespan)
allowed_origins = [origin for origin in os.environ.get("RED_POTATO_RADAR_ALLOWED_ORIGINS", "").split(",") if origin]
if allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"])


@app.middleware("http")
async def bearer_authentication(request: Request, call_next):
    """Bearer authentication is opt-in locally and mandatory for declared public use."""
    token = os.environ.get("RED_POTATO_RADAR_API_TOKEN")
    if request.url.path.startswith("/api") and request.url.path != "/api/health" and token:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"detail": "未授权"}, status_code=401)
    if request.url.path.startswith("/api") and getattr(request.app.state, "maintenance", False):
        return JSONResponse({"detail": "数据库维护中"}, status_code=503)
    return await call_next(request)


def service() -> MonitoringService:
    return app.state.service


def backup_service() -> BackupService:
    return app.state.backup_service


def backup_settings() -> dict[str, str]:
    settings = service().db.settings()
    return {
        "backup_enabled": settings.get("backup_enabled", "false"),
        "backup_schedule": settings.get("backup_schedule", "daily"),
        "backup_daily_time": settings.get("backup_daily_time", "03:00"),
        "backup_weekday": settings.get("backup_weekday", "6"),
        "backup_weekly_time": settings.get("backup_weekly_time", "03:00"),
        "backup_keep_count": settings.get("backup_keep_count", "20"),
    }


def backup_record_public(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in {"file_path", "error_detail"}}


def import_max_bytes() -> int:
    try:
        value = int(os.environ.get("RED_POTATO_RADAR_IMPORT_MAX_BYTES", str(DEFAULT_IMPORT_MAX_BYTES)))
    except ValueError:
        return DEFAULT_IMPORT_MAX_BYTES
    return min(max(value, 1), 10 * 1024 * 1024 * 1024)


def require_backup_csrf(request: Request) -> None:
    cookie = request.cookies.get("backup_csrf")
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(403, "CSRF 校验失败，请刷新页面后重试")


def require_backup_same_site(request: Request) -> None:
    if not request.cookies.get("backup_csrf"):
        raise HTTPException(403, "CSRF 校验失败，请刷新页面后重试")


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
    async with app.state.write_lock:
        return {"added": service().confirm_products(request.products)}


@app.delete("/api/products")
async def delete_products(product_ids: Annotated[list[str], Query()]) -> dict:
    async with app.state.write_lock:
        return {"deleted": service().db.delete_products(product_ids)}


@app.post("/api/collect")
async def collect(request: CollectRequest, background: BackgroundTasks) -> dict:
    if service()._lock.locked(): return {"state": "busy"}
    generation = app.state.maintenance_generation
    async def queued_collection() -> None:
        async with app.state.write_lock:
            if app.state.maintenance or generation != app.state.maintenance_generation:
                return
            await service().collect_products(request.product_ids)
    background.add_task(queued_collection)
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


@app.get("/api/notification-shops")
async def notification_shops() -> dict:
    selected = set(filter(None, service().db.get_setting("wecom_shop_ids", "").split(",")))
    return {
        "shops": [{"shop_id": row.get("shop_id"), "shop_name": row["shop_name"]} for row in service().shops() if row.get("shop_id")],
        "selected_shop_ids": sorted(selected),
    }


@app.put("/api/notification-shops")
async def set_notification_shops(request: NotificationShopsRequest) -> dict:
    selected = {shop_id.strip() for shop_id in request.shop_ids if shop_id.strip()}
    available = {str(row["shop_id"]) for row in service().shops() if row.get("shop_id")}
    if selected - available:
        raise HTTPException(422, "通知店铺中存在已不存在的店铺，请刷新后重试")
    async with app.state.write_lock:
        service().db.set_setting("wecom_shop_ids", ",".join(sorted(selected)))
    return {"selected_shop_ids": sorted(selected)}


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
    async with app.state.write_lock:
        db = service().db
        updates = {key: value for key, value in request.values.items() if key != "wecom_shop_ids" and value is not None and not (key in ("wecom_webhook", "feishu_webhook") and is_masked(value))}
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
        backup_scheduler = getattr(app.state, "backup_scheduler", None)
        if backup_scheduler:
            backup_scheduler.reconfigure()
            if merged.get("backup_enabled") == "true": backup_scheduler.start()
            else: await backup_scheduler.stop()
        return service().settings_public()


@app.post("/api/settings/test-notify")
async def test_notify() -> dict:
    resolved = resolve_sender(service().db.settings())
    if not resolved: raise HTTPException(400, "尚未启用通知或所选渠道未配置 Webhook")
    channel, webhook, sender = resolved
    try:
        await sender(webhook, test_message(channel))
    except Exception as exc:
        async with app.state.write_lock:
            service().db.log("error", channel, "测试通知失败", detail=repr(exc))
        raise HTTPException(502, "发送失败，详情已写入运行日志") from exc
    return {"ok": True}


@app.get("/api/logs")
async def logs(level: str | None = None, limit: int = 500, source: str | None = None) -> list[dict]:
    return service().db.logs(level, limit, source)


@app.delete("/api/logs")
async def clear_logs() -> dict:
    async with app.state.write_lock:
        return {"deleted": service().db.clear_logs()}


@app.post("/api/cleanup")
async def cleanup(request: CleanRequest) -> dict:
    now = datetime.now(TZ)
    async with app.state.maintenance_lock:
        async with app.state.write_lock:
            async with service()._lock:
                return await run_blocking(service().db.cleanup, (now - timedelta(days=request.snapshot_days)).isoformat(), (now - timedelta(days=request.log_days)).isoformat())


@app.get("/api/backup/csrf")
async def backup_csrf() -> JSONResponse:
    token = secrets.token_urlsafe(32)
    response = JSONResponse({"csrf_token": token})
    response.set_cookie("backup_csrf", token, httponly=False, samesite="strict")
    return response


async def run_blocking(operation, *args):
    """Keep maintenance locks held until a worker thread has reached a safe boundary."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@app.get("/api/backups")
async def backups(request: Request) -> JSONResponse:
    records = service().db.backup_records()
    response = JSONResponse({
        "directory": str(backup_service().directory),
        "records": [backup_record_public(record) for record in records],
        "settings": backup_settings(),
        "busy": app.state.maintenance_lock.locked(),
    })
    if not request.cookies.get("backup_csrf"):
        response.set_cookie("backup_csrf", secrets.token_urlsafe(32), httponly=False, samesite="strict")
    return response


@app.post("/api/backups")
async def create_backup(request: Request) -> dict:
    require_backup_same_site(request)
    if app.state.maintenance_lock.locked():
        raise HTTPException(409, "已有备份、恢复或清理任务正在执行")
    async with app.state.maintenance_lock:
        async with app.state.write_lock:
            record = await run_blocking(backup_service().create, "manual")
    if record["status"] != "success":
        raise HTTPException(500, "备份失败，详情已写入运行日志")
    return backup_record_public(record)


@app.put("/api/backups/import")
async def import_backup(request: Request, filename: str = Query(min_length=1, max_length=255)) -> dict:
    require_backup_same_site(request)
    if Path(filename).suffix.lower() not in IMPORT_EXTENSIONS:
        raise HTTPException(422, "仅支持 .db、.sqlite 或 .sqlite3 SQLite 数据库文件")
    if app.state.maintenance_lock.locked():
        raise HTTPException(409, "已有备份、恢复或清理任务正在执行")
    maximum = import_max_bytes()
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > maximum:
                raise HTTPException(413, f"导入文件不能超过 {maximum // 1024 // 1024}MB")
        except ValueError as exc:
            raise HTTPException(400, "无效的 Content-Length") from exc
    expected_size = int(declared_size) if declared_size else maximum
    if shutil.disk_usage(app.state.paths.data_dir).free < expected_size * 3:
        raise HTTPException(507, "服务端可用磁盘空间不足，无法安全导入该数据库")
    if app.state.import_lock.locked():
        raise HTTPException(409, "已有外部数据库上传任务正在执行")
    temporary = app.state.paths.backup_imports / f"{secrets.token_urlsafe(16)}.upload"
    size = 0
    try:
        async with app.state.import_lock:
            async with app.state.maintenance_lock:
                with temporary.open("wb") as destination:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > maximum:
                            raise HTTPException(413, f"导入文件不能超过 {maximum // 1024 // 1024}MB")
                        destination.write(chunk)
                async with app.state.write_lock:
                    result = await run_blocking(backup_service().import_external, temporary, filename, maximum)
        return {"record": backup_record_public(result["record"]), "preview": result["preview"]}
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        try:
            async with app.state.write_lock:
                service().db.log("error", "backup", "外部数据库导入失败", detail=repr(exc))
        except OSError:
            pass
        raise HTTPException(507, "导入失败：服务端磁盘或文件系统不可用") from exc
    finally:
        temporary.unlink(missing_ok=True)


@app.get("/api/backups/{record_id}/download")
async def download_backup(record_id: int):
    try:
        path = backup_service().download_path(record_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/x-sqlite3", filename=path.name)


@app.delete("/api/backups/{record_id}")
async def delete_backup(record_id: int) -> dict:
    if app.state.maintenance_lock.locked():
        raise HTTPException(409, "已有备份、恢复或清理任务正在执行")
    async with app.state.maintenance_lock:
        try:
            async with app.state.write_lock:
                await run_blocking(backup_service().delete, record_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
    return {"deleted": record_id}


@app.post("/api/backups/{record_id}/restore-confirmation")
async def restore_confirmation(record_id: int, request: Request) -> dict:
    require_backup_csrf(request)
    try:
        backup_service().download_path(record_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    now = datetime.now(TZ)
    app.state.restore_tokens = {token: value for token, value in app.state.restore_tokens.items() if value[1] > now}
    token = secrets.token_urlsafe(32)
    app.state.restore_tokens[token] = (record_id, now + timedelta(minutes=5))
    return {"confirmation_token": token}


@app.post("/api/backups/{record_id}/restore")
async def restore_backup(record_id: int, request: Request, payload: RestoreRequest) -> dict:
    require_backup_csrf(request)
    confirmation = app.state.restore_tokens.pop(payload.confirmation_token, None)
    if not confirmation or confirmation[0] != record_id or confirmation[1] <= datetime.now(TZ):
        raise HTTPException(400, "恢复确认已过期，请重新确认")
    if app.state.restore_lock.locked() or app.state.maintenance_lock.locked():
        raise HTTPException(409, "已有备份、恢复或清理任务正在执行")
    async with app.state.restore_lock:
        backup_scheduler = app.state.backup_scheduler
        # Stop before claiming the maintenance lock so an automatic job cannot wait on it.
        await backup_scheduler.stop()
        scheduler = app.state.scheduler
        try:
            async with app.state.maintenance_lock:
                app.state.maintenance = True
                app.state.maintenance_generation += 1
                try:
                    await scheduler.stop()
                    await scheduler.wait_for_jobs()
                    async with app.state.write_lock:
                        async with service()._lock:
                            result = await run_blocking(backup_service().restore, record_id)
                    if service().db.get_setting("monitoring_enabled") == "true":
                        scheduler.start()
                    return {"record": backup_record_public(result["record"]), "safety_backup": backup_record_public(result["safety_backup"])}
                except FileNotFoundError as exc:
                    raise HTTPException(404, str(exc)) from exc
                except RuntimeError as exc:
                    service().db.log("error", "backup", "数据库恢复失败", detail=repr(exc))
                    raise HTTPException(500, str(exc)) from exc
                except Exception as exc:
                    service().db.log("error", "backup", "数据库恢复失败", detail=repr(exc))
                    raise HTTPException(500, "数据库恢复失败，详情已写入运行日志") from exc
                finally:
                    app.state.maintenance = False
                    if service().db.get_setting("monitoring_enabled") == "true":
                        scheduler.start()
        finally:
            backup_scheduler.reconfigure()
            if service().db.get_setting("backup_enabled") == "true":
                backup_scheduler.start()


@app.get("/api/duplicates")
async def duplicates() -> list[list[dict]]:
    return service().smart_duplicates()


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        target = WEB_DIST / path
        return FileResponse(target) if path and target.is_file() else FileResponse(WEB_DIST / "index.html")
