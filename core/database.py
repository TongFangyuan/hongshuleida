from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo

from .config import SHANGHAI_TZ
from .models import ProductData, Snapshot


TZ = ZoneInfo(SHANGHAI_TZ)


class Database:
    """SQLite repository. Every connection is short-lived and WAL-safe."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS products (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, shop_name TEXT, shop_id TEXT,
          cover TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
          last_error TEXT, delisted_at TEXT, delisted_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          product_id TEXT NOT NULL, captured_at TEXT NOT NULL, sold INTEGER NOT NULL,
          shop_sold INTEGER, price REAL, fans INTEGER, stock_status INTEGER, deliverable INTEGER,
          PRIMARY KEY (product_id, captured_at),
          FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_product_time ON snapshots(product_id, captured_at);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL,
          source TEXT NOT NULL, product_id TEXT, product_title TEXT, message TEXT NOT NULL, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(created_at DESC);
        CREATE TABLE IF NOT EXISTS deleted_products (product_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS backup_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL, file_path TEXT NOT NULL,
          created_at TEXT NOT NULL, trigger_type TEXT NOT NULL, file_size INTEGER,
          status TEXT NOT NULL, error_detail TEXT, restored_at TEXT, deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_backup_records_created_at ON backup_records(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_backup_records_status ON backup_records(status);
        """
        with self.transaction() as conn:
            conn.executescript(schema)
            self._add_missing_columns(conn, "products", {
                "shop_name": "TEXT", "shop_id": "TEXT", "cover": "TEXT", "active": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT", "last_error": "TEXT", "delisted_at": "TEXT", "delisted_reason": "TEXT"
            })
            self._add_missing_columns(conn, "snapshots", {
                "shop_sold": "INTEGER", "price": "REAL", "fans": "INTEGER", "stock_status": "INTEGER", "deliverable": "INTEGER"
            })

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @staticmethod
    def _now() -> str:
        return datetime.now(TZ).isoformat(timespec="seconds")

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect(readonly=True) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def settings(self) -> dict[str, str]:
        with self._connect(readonly=True) as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}

    def upsert_product(self, product: ProductData) -> None:
        with self.transaction() as conn:
            conn.execute("""INSERT INTO products(id,title,shop_name,shop_id,cover,active,created_at,last_error,delisted_at,delisted_reason)
            VALUES(?,?,?,?,?,1,?,NULL,NULL,NULL)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,shop_name=excluded.shop_name,shop_id=excluded.shop_id,
              cover=excluded.cover,active=1,last_error=NULL,delisted_at=NULL,delisted_reason=NULL""",
              (product.id, product.title, product.shop_name, product.shop_id, product.cover, self._now()))

    def save_snapshot(self, snapshot: Snapshot) -> None:
        with self.transaction() as conn:
            conn.execute("""INSERT INTO snapshots(product_id,captured_at,sold,shop_sold,price,fans,stock_status,deliverable)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(product_id,captured_at) DO UPDATE SET
            sold=excluded.sold,shop_sold=excluded.shop_sold,price=excluded.price,fans=excluded.fans,
            stock_status=excluded.stock_status,deliverable=excluded.deliverable""",
              (snapshot.product_id, snapshot.captured_at.astimezone(TZ).isoformat(timespec="seconds"), snapshot.sold,
               snapshot.shop_sold, snapshot.price, snapshot.fans, snapshot.stock_status,
               None if snapshot.deliverable is None else int(snapshot.deliverable)))

    def mark_error(self, product_id: str, message: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE products SET last_error=? WHERE id=?", (message[:1000], product_id))

    def mark_delisted(self, product_id: str, reason: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE products SET active=0,delisted_at=?,delisted_reason=?,last_error=NULL WHERE id=?",
                         (self._now(), reason, product_id))

    def clear_error(self, product_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE products SET last_error=NULL WHERE id=?", (product_id,))

    def products(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY created_at DESC"
        with self._connect(readonly=True) as conn:
            return [dict(row) for row in conn.execute(sql)]

    def product(self, product_id: str) -> dict[str, Any] | None:
        with self._connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None

    def snapshots(self, product_id: str, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        clauses, args = ["product_id=?"], [product_id]
        if start: clauses.append("captured_at>=?"); args.append(start.astimezone(TZ).isoformat())
        if end: clauses.append("captured_at<?"); args.append(end.astimezone(TZ).isoformat())
        with self._connect(readonly=True) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM snapshots WHERE " + " AND ".join(clauses) + " ORDER BY captured_at", args)]

    def all_snapshots(self, product_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        if not product_ids: return {}
        placeholders = ",".join("?" for _ in product_ids)
        with self._connect(readonly=True) as conn:
            rows = conn.execute(f"SELECT * FROM snapshots WHERE product_id IN ({placeholders}) ORDER BY captured_at", list(product_ids)).fetchall()
        result: dict[str, list[dict[str, Any]]] = {pid: [] for pid in product_ids}
        for row in rows: result[row["product_id"]].append(dict(row))
        return result

    def delete_products(self, product_ids: Sequence[str]) -> int:
        if not product_ids: return 0
        with self.transaction() as conn:
            for pid in product_ids:
                conn.execute("INSERT INTO deleted_products(product_id,deleted_at) VALUES(?,?) ON CONFLICT(product_id) DO UPDATE SET deleted_at=excluded.deleted_at", (pid, self._now()))
            result = conn.execute(f"DELETE FROM products WHERE id IN ({','.join('?' for _ in product_ids)})", list(product_ids))
            return result.rowcount

    def log(self, level: str, source: str, message: str, product_id: str | None = None, product_title: str | None = None, detail: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO logs(created_at,level,source,product_id,product_title,message,detail) VALUES(?,?,?,?,?,?,?)",
                         (self._now(), level, source, product_id, product_title, message, detail))

    def logs(self, level: str | None = None, limit: int = 500, source: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM logs", []
        clauses = []
        if level: clauses.append("level=?"); args.append(level)
        if source: clauses.append("source=?"); args.append(source)
        if clauses: sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(min(max(limit, 1), 2000))
        with self._connect(readonly=True) as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def add_backup_record(self, file_name: str, file_path: str, trigger_type: str, status: str,
                          file_size: int | None = None, error_detail: str | None = None) -> dict[str, Any]:
        with self.transaction() as conn:
            cursor = conn.execute("""INSERT INTO backup_records
                (file_name,file_path,created_at,trigger_type,file_size,status,error_detail)
                VALUES(?,?,?,?,?,?,?)""",
                (file_name, file_path, self._now(), trigger_type, file_size, status, error_detail))
            row = conn.execute("SELECT * FROM backup_records WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def backup_records(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM backup_records" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY id DESC"
        with self._connect(readonly=True) as conn:
            return [dict(row) for row in conn.execute(sql)]

    def backup_record(self, record_id: int) -> dict[str, Any] | None:
        with self._connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM backup_records WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def backup_record_by_path(self, file_path: str) -> dict[str, Any] | None:
        with self._connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM backup_records WHERE file_path=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (file_path,)).fetchone()
        return dict(row) if row else None

    def mark_backup_deleted(self, record_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE backup_records SET status='deleted', deleted_at=? WHERE id=?", (self._now(), record_id))

    def mark_backup_restored(self, record_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE backup_records SET restored_at=? WHERE id=?", (self._now(), record_id))

    def clear_logs(self) -> int:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM logs").rowcount

    def data_usage(self) -> dict[str, int]:
        files = [self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")]
        size = sum(p.stat().st_size for p in files if p.exists())
        with self._connect(readonly=True) as conn:
            count = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        return {"bytes": size, "snapshots": count}

    def cleanup(self, snapshot_before: str, logs_before: str) -> dict[str, int]:
        """Keep one pre-cutoff snapshot per product as analytical baseline."""
        with self.transaction() as conn:
            baseline = conn.execute("""SELECT count(*) FROM snapshots s WHERE captured_at < ? AND rowid IN
              (SELECT rowid FROM snapshots old WHERE old.product_id=s.product_id AND old.captured_at=(SELECT max(x.captured_at) FROM snapshots x WHERE x.product_id=s.product_id AND x.captured_at < ?))""", (snapshot_before, snapshot_before)).fetchone()[0]
            deleted = conn.execute("""DELETE FROM snapshots WHERE captured_at < ? AND rowid NOT IN
              (SELECT rowid FROM snapshots old WHERE old.captured_at=(SELECT max(x.captured_at) FROM snapshots x WHERE x.product_id=old.product_id AND x.captured_at < ?))""", (snapshot_before, snapshot_before)).rowcount
            log_deleted = conn.execute("DELETE FROM logs WHERE created_at < ?", (logs_before,)).rowcount
        # SQLite cannot checkpoint or VACUUM while this write transaction is open.
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with self._connect() as conn:
            conn.execute("VACUUM")
        return {"snapshots_deleted": deleted, "logs_deleted": log_deleted, "baselines_kept": baseline}
