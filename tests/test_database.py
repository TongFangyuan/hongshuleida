import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.database import Database
from core.models import ProductData, Snapshot

TZ = ZoneInfo("Asia/Shanghai")


def test_migration_snapshot_and_deletion(tmp_path: Path) -> None:
    path = tmp_path / "monitor.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE products (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute("CREATE TABLE snapshots (product_id TEXT, captured_at TEXT, sold INTEGER, PRIMARY KEY(product_id,captured_at))")
    conn.commit(); conn.close()
    db = Database(path)
    product = ProductData("a" * 24, "测试商品", "测试店", "shop", None, 1, 1, 9.9, 1, None, True)
    db.upsert_product(product)
    db.save_snapshot(Snapshot(product.id, datetime(2026, 9, 5, tzinfo=TZ), 1, 1, 9.9, 1, None, True))
    assert db.product(product.id)["cover"] is None
    assert len(db.snapshots(product.id)) == 1
    assert db.delete_products([product.id]) == 1
    assert db.product(product.id) is None
    assert db.products() == []


def test_cleanup_keeps_one_old_baseline(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.db")
    product = ProductData("b" * 24, "商品", None, None, None, 0, None, None, None, None, None)
    db.upsert_product(product)
    for day, sold in ((1, 1), (2, 2), (3, 3)):
        db.save_snapshot(Snapshot(product.id, datetime(2026, 1, day, tzinfo=TZ), sold, None, None, None, None, None))
    result = db.cleanup("2026-01-03T00:00:00+08:00", "2026-01-01T00:00:00+08:00")
    assert result["baselines_kept"] == 1
    assert len(db.snapshots(product.id)) == 2
