from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import SHANGHAI_TZ
from .database import Database


TZ = ZoneInfo(SHANGHAI_TZ)
SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_IMPORT_TABLES = {"products", "snapshots", "settings"}
SENSITIVE_SETTING_KEYS = ("wecom_webhook", "feishu_webhook")
WEBHOOK_URL = re.compile(r"https://(?:qyapi\.weixin\.qq\.com|open\.feishu\.cn)/\S+")


class BackupService:
    """Creates verified SQLite backups without copying live WAL files."""

    def __init__(self, database: Database, directory: Path):
        self.db = database
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _destination(self, prefix: str = "monitor") -> Path:
        stem = datetime.now(TZ).strftime(f"{prefix}_%Y%m%d_%H%M%S")
        destination = self.directory / f"{stem}.db"
        suffix = 1
        while destination.exists():
            destination = self.directory / f"{stem}_{suffix}.db"
            suffix += 1
        return destination

    @staticmethod
    def _verify(path: Path) -> None:
        with sqlite3.connect(path, timeout=10) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"备份完整性校验失败：{result}")

    @staticmethod
    def _copy_database(source_path: Path, target_path: Path) -> None:
        with sqlite3.connect(source_path, timeout=10) as source, sqlite3.connect(target_path, timeout=10) as target:
            source.execute("PRAGMA busy_timeout=10000")
            source.backup(target)

    @staticmethod
    def _finalize_standalone(path: Path) -> None:
        """Make a portable .db that does not depend on a sidecar WAL file."""
        with sqlite3.connect(path, timeout=10) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        Path(f"{path}-wal").unlink(missing_ok=True)
        Path(f"{path}-shm").unlink(missing_ok=True)

    @staticmethod
    def _sanitize_sensitive_data(path: Path) -> None:
        with sqlite3.connect(path, timeout=10) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "settings" in tables:
                secrets = [row[0] for row in conn.execute("SELECT value FROM settings WHERE key IN (?, ?)", SENSITIVE_SETTING_KEYS)]
                conn.execute("DELETE FROM settings WHERE key IN (?, ?)", SENSITIVE_SETTING_KEYS)
                if "logs" in tables:
                    for row in conn.execute("SELECT id,message,detail FROM logs"):
                        message = row[1] or ""
                        detail = row[2] or ""
                        for secret in secrets:
                            message = message.replace(secret, "[已隐藏]")
                            detail = detail.replace(secret, "[已隐藏]")
                        conn.execute("UPDATE logs SET message=?,detail=? WHERE id=?", (WEBHOOK_URL.sub("[已隐藏]", message), WEBHOOK_URL.sub("[已隐藏]", detail), row[0]))
                conn.commit()
                # Deletion alone leaves credential bytes in SQLite free pages.
                conn.execute("VACUUM")

    @staticmethod
    def _validate_required_schema(conn: sqlite3.Connection) -> None:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_IMPORT_TABLES - tables
        if missing:
            raise RuntimeError(f"导入文件缺少必要业务表：{', '.join(sorted(missing))}")
        required_columns = {
            "products": {"id", "title"},
            "snapshots": {"product_id", "captured_at", "sold"},
            "settings": {"key", "value"},
        }
        for table, expected in required_columns.items():
            columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            absent = expected - columns.keys()
            if absent:
                raise RuntimeError(f"导入文件的 {table} 表缺少字段：{', '.join(sorted(absent))}")
        product_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(products)")}
        snapshot_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(snapshots)")}
        settings_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(settings)")}
        if product_columns["id"][5] != 1 or snapshot_columns["product_id"][5] != 1 or snapshot_columns["captured_at"][5] != 2 or settings_columns["key"][5] != 1:
            raise RuntimeError("导入文件的主键结构不兼容")

    @staticmethod
    def inspect_external(path: Path, max_bytes: int) -> dict:
        size = path.stat().st_size
        if not 0 < size <= max_bytes:
            raise RuntimeError(f"导入文件大小必须在 1 字节到 {max_bytes // 1024 // 1024}MB 之间")
        with path.open("rb") as source:
            if source.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise RuntimeError("导入文件不是有效的 SQLite 数据库")
        try:
            with sqlite3.connect(path, timeout=10) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"导入文件完整性校验失败：{integrity}")
                BackupService._validate_required_schema(conn)
                product_count = conn.execute("SELECT count(*) FROM products").fetchone()[0]
                snapshot_count = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                range_row = conn.execute("SELECT min(captured_at), max(captured_at) FROM snapshots").fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("导入文件无法作为 SQLite 数据库读取") from exc
        return {
            "file_size": size,
            "product_count": product_count,
            "snapshot_count": snapshot_count,
            "oldest_snapshot_at": range_row[0],
            "latest_snapshot_at": range_row[1],
            "integrity": "ok",
        }

    def _copy_external_data(self, source_path: Path, target: Database) -> None:
        with sqlite3.connect(source_path, timeout=10) as source:
            source.row_factory = sqlite3.Row
            source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            now = datetime.now(TZ).isoformat(timespec="seconds")
            with target.transaction() as destination:
                for row in source.execute("SELECT * FROM products"):
                    keys = set(row.keys())
                    destination.execute("""INSERT INTO products
                        (id,title,shop_name,shop_id,cover,active,created_at,last_error,delisted_at,delisted_reason)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                        row["id"], row["title"], row["shop_name"] if "shop_name" in keys else None,
                        row["shop_id"] if "shop_id" in keys else None, row["cover"] if "cover" in keys else None,
                        row["active"] if "active" in keys else 1, row["created_at"] if "created_at" in keys else now,
                        row["last_error"] if "last_error" in keys else None, row["delisted_at"] if "delisted_at" in keys else None,
                        row["delisted_reason"] if "delisted_reason" in keys else None,
                    ))
                for row in source.execute("SELECT * FROM snapshots"):
                    keys = set(row.keys())
                    destination.execute("""INSERT INTO snapshots
                        (product_id,captured_at,sold,shop_sold,price,fans,stock_status,deliverable)
                        VALUES(?,?,?,?,?,?,?,?)""", (
                        row["product_id"], row["captured_at"], row["sold"], row["shop_sold"] if "shop_sold" in keys else None,
                        row["price"] if "price" in keys else None, row["fans"] if "fans" in keys else None,
                        row["stock_status"] if "stock_status" in keys else None, row["deliverable"] if "deliverable" in keys else None,
                    ))
                for row in source.execute("SELECT key,value FROM settings"):
                    if row["key"] not in SENSITIVE_SETTING_KEYS:
                        destination.execute("INSERT INTO settings(key,value) VALUES(?,?)", (row["key"], row["value"]))
                if "deleted_products" in source_tables:
                    columns = {row[1] for row in source.execute("PRAGMA table_info(deleted_products)")}
                    if {"product_id", "deleted_at"}.issubset(columns):
                        for row in source.execute("SELECT product_id,deleted_at FROM deleted_products"):
                            destination.execute("INSERT OR REPLACE INTO deleted_products(product_id,deleted_at) VALUES(?,?)", (row["product_id"], row["deleted_at"]))

    def import_external(self, temporary: Path, original_name: str, max_bytes: int) -> dict:
        staging: Path | None = None
        try:
            preview = self.inspect_external(temporary, max_bytes)
            destination = self._destination("import")
            staging = destination.with_suffix(".tmp")
            target = Database(staging)
            self._copy_external_data(temporary, target)
            self._sanitize_sensitive_data(staging)
            self._finalize_standalone(staging)
            self._verify(staging)
            os.replace(staging, destination)
            record = self.db.add_backup_record(
                destination.name, str(destination), "external_import", "success", destination.stat().st_size
            )
            self.db.log("success", "backup", "外部数据库导入成功", detail=original_name)
            self.apply_retention()
            return {"record": record, "preview": preview}
        except Exception as exc:
            self.db.log("error", "backup", "外部数据库导入失败", detail=repr(exc))
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if staging:
                staging.unlink(missing_ok=True)
                Path(f"{staging}-wal").unlink(missing_ok=True)
                Path(f"{staging}-shm").unlink(missing_ok=True)

    def _safe_path(self, record: dict) -> Path:
        path = Path(record["file_path"]).resolve()
        if path.parent != self.directory.resolve() or path.name != record["file_name"] or not path.name.startswith(("monitor_", "import_")) or path.suffix != ".db":
            raise RuntimeError("备份文件路径无效")
        return path

    def create(self, trigger_type: str, apply_retention: bool = True) -> dict:
        destination = self._destination()
        temporary = destination.with_suffix(".tmp")
        try:
            self._copy_database(self.db.path, temporary)
            self._sanitize_sensitive_data(temporary)
            self._finalize_standalone(temporary)
            self._verify(temporary)
            os.replace(temporary, destination)
            record = self.db.add_backup_record(destination.name, str(destination), trigger_type, "success", destination.stat().st_size)
            self.db.log("success", "backup", "数据库备份成功", detail=destination.name)
            if apply_retention:
                self.apply_retention()
            return record
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            record = self.db.add_backup_record(destination.name, str(destination), trigger_type, "failed", error_detail=repr(exc))
            self.db.log("error", "backup", "数据库备份失败", detail=repr(exc))
            return record

    def apply_retention(self) -> None:
        keep = min(max(int(self.db.get_setting("backup_keep_count", "20") or 20), 1), 100)
        records = [record for record in self.db.backup_records() if record["status"] == "success"]
        for record in records[keep:]:
            try:
                path = self._safe_path(record)
                if path.exists():
                    path.unlink()
                self.db.mark_backup_deleted(record["id"])
                self.db.log("info", "backup", "自动清理过期备份", detail=record["file_name"])
            except Exception as exc:
                self.db.log("error", "backup", "自动清理备份失败", detail=repr(exc))

    def delete(self, record_id: int) -> None:
        record = self._available_record(record_id)
        path = self._safe_path(record)
        if path.exists():
            path.unlink()
        self.db.mark_backup_deleted(record_id)
        self.db.log("info", "backup", "删除备份文件", detail=record["file_name"])

    def download_path(self, record_id: int) -> Path:
        record = self._available_record(record_id)
        path = self._safe_path(record)
        if not path.is_file():
            raise FileNotFoundError("备份文件不存在")
        return path

    def restore(self, record_id: int) -> dict:
        record = self._available_record(record_id)
        source = self.download_path(record_id)
        self._verify(source)
        preserved_secrets = {key: self.db.get_setting(key) for key in SENSITIVE_SETTING_KEYS}
        safety = self.create("pre_restore", apply_retention=False)
        if safety["status"] != "success":
            raise RuntimeError("恢复前安全备份失败，已取消恢复")
        temporary = self.db.path.with_suffix(".restore.tmp")
        rollback = self.db.path.with_suffix(".restore.rollback.tmp")
        replaced = False
        try:
            with sqlite3.connect(source, timeout=10) as backup, sqlite3.connect(temporary, timeout=10) as target:
                backup.backup(target)
            self._finalize_standalone(temporary)
            self._verify(temporary)
            # Private rollback copy is never exposed as a downloadable backup.
            self._copy_database(self.db.path, rollback)
            self._finalize_standalone(rollback)
            self._verify(rollback)
            # A truncated WAL makes the old main database self-contained before replacement.
            # If the process stops here, either the old or new complete database remains usable.
            with sqlite3.connect(self.db.path, timeout=10) as current:
                current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            Path(f"{self.db.path}-wal").unlink(missing_ok=True)
            Path(f"{self.db.path}-shm").unlink(missing_ok=True)
            os.replace(temporary, self.db.path)
            replaced = True
            self.db.migrate()
            for key, value in preserved_secrets.items():
                if value is not None:
                    self.db.set_setting(key, value)
            self.reconcile_records()
            restored_record = self.db.backup_record_by_path(str(source))
            if restored_record is None:
                raise RuntimeError("恢复后无法重建备份审计记录")
            self.db.mark_backup_restored(restored_record["id"])
            self.db.log("success", "backup", "数据库恢复成功", detail=record["file_name"])
            self.apply_retention()
            return {"record": record, "safety_backup": safety}
        except Exception:
            if replaced:
                Path(f"{self.db.path}-wal").unlink(missing_ok=True)
                Path(f"{self.db.path}-shm").unlink(missing_ok=True)
                os.replace(rollback, self.db.path)
                self.db.migrate()
            temporary.unlink(missing_ok=True)
            raise
        finally:
            rollback.unlink(missing_ok=True)

    def _available_record(self, record_id: int) -> dict:
        record = self.db.backup_record(record_id)
        if not record or record["deleted_at"] or record["status"] != "success":
            raise FileNotFoundError("可用备份不存在")
        return record

    def reconcile_records(self) -> None:
        """Reconcile restored audit metadata with files that were not rolled back."""
        known = {record["file_path"]: record for record in self.db.backup_records()}
        for record in known.values():
            if record["status"] == "success" and not Path(record["file_path"]).is_file():
                self.db.mark_backup_deleted(record["id"])
        for path in self.directory.glob("*.db"):
            if not path.name.startswith(("monitor_", "import_")):
                continue
            if str(path) in known:
                continue
            try:
                self._verify(path)
                self.db.add_backup_record(path.name, str(path), "recovered", "success", path.stat().st_size)
            except Exception as exc:
                self.db.add_backup_record(path.name, str(path), "recovered", "failed", error_detail=repr(exc))


class BackupScheduler:
    """Small settings-driven scheduler for daily, weekly, or startup backups."""

    def __init__(self, job: Callable[[str], Awaitable[object]], settings: Callable[[], dict[str, str]]):
        self.job = job
        self.settings = settings
        self.running = False
        self.next_run_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._startup_ran = False
        self._executing = False

    async def _sleep(self, seconds: float) -> bool:
        if self._wake is None:
            self._wake = asyncio.Event()
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(seconds, 0.1))
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _time(value: str) -> tuple[int, int]:
        hour, minute = (int(part) for part in value.split(":"))
        return hour, minute

    def _next_run(self, config: dict[str, str]) -> datetime | None:
        schedule = config.get("backup_schedule", "daily")
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        if schedule == "startup":
            return None
        time_value = config.get("backup_daily_time", "03:00") if schedule == "daily" else config.get("backup_weekly_time", "03:00")
        hour, minute = self._time(time_value)
        candidate = now.replace(hour=hour, minute=minute)
        if schedule == "weekly":
            weekday = int(config.get("backup_weekday", "6"))
            candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= now:
            candidate += timedelta(days=1 if schedule == "daily" else 7)
        return candidate

    async def _run(self) -> None:
        try:
            while self.running:
                config = self.settings()
                if config.get("backup_enabled") != "true":
                    self.next_run_at = None
                    await self._sleep(3600)
                    continue
                if config.get("backup_schedule", "daily") == "startup":
                    self.next_run_at = None
                    if not self._startup_ran:
                        self._startup_ran = True
                        await self._run_job("startup")
                    await self._sleep(3600)
                    continue
                next_run = self._next_run(config)
                self.next_run_at = next_run
                if next_run is None or await self._sleep((next_run - datetime.now(TZ)).total_seconds()):
                    continue
                if not self.running:
                    continue
                self.next_run_at = None
                await self._run_job("daily" if config.get("backup_schedule") == "daily" else "weekly")
        except asyncio.CancelledError:
            raise
        finally:
            self.next_run_at = None

    async def _run_job(self, trigger_type: str) -> None:
        self._executing = True
        try:
            await self.job(trigger_type)
        except Exception:
            pass
        finally:
            self._executing = False

    def start(self) -> None:
        if not self.running:
            self.running = True
            self._task = asyncio.create_task(self._run())

    def reconfigure(self) -> None:
        if self._wake:
            self._wake.set()

    async def stop(self) -> None:
        self.running = False
        if self._wake:
            self._wake.set()
        if self._task:
            await self._task
        self._task = None
