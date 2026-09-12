import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.backup import BackupScheduler, BackupService
from core.database import Database


def test_online_backup_retention_and_restore(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.db")
    backups = BackupService(db, tmp_path / "backups")
    db.set_setting("backup_keep_count", "2")
    db.set_setting("restore_marker", "before")
    db.set_setting("wecom_webhook", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret")

    first = backups.create("manual")
    assert first["status"] == "success"
    with sqlite3.connect(first["file_path"]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM settings WHERE key='wecom_webhook'").fetchone() is None
    assert b"key=secret" not in Path(first["file_path"]).read_bytes()

    db.set_setting("restore_marker", "after")
    second = backups.create("daily")
    third = backups.create("weekly")
    assert second["status"] == third["status"] == "success"
    assert len([item for item in db.backup_records() if item["status"] == "success"]) == 2

    backups.restore(second["id"])
    assert db.get_setting("restore_marker") == "after"
    assert db.get_setting("wecom_webhook").endswith("key=secret")
    restored = db.backup_record_by_path(second["file_path"])
    assert restored and restored["restored_at"]
    assert db.logs(source="backup")


def test_backup_api_download_confirmation_and_restore(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RED_POTATO_RADAR_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from server.main import app

    with TestClient(app) as client:
        assert client.get("/api/backups").status_code == 200
        app.state.db.set_setting("restore_marker", "before")
        created = client.post("/api/backups")
        assert created.status_code == 200
        record = created.json()
        app.state.db.set_setting("restore_marker", "after")

        downloaded = client.get(f"/api/backups/{record['id']}/download")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("application/x-sqlite3")
        assert downloaded.content.startswith(b"SQLite format 3")

        assert client.post(f"/api/backups/{record['id']}/restore-confirmation").status_code == 403
        csrf = client.get("/api/backup/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        confirmation = client.post(f"/api/backups/{record['id']}/restore-confirmation", headers=headers)
        assert confirmation.status_code == 200
        restored = client.post(f"/api/backups/{record['id']}/restore", headers=headers, json=confirmation.json())
        assert restored.status_code == 200
        assert app.state.db.get_setting("restore_marker") == "before"

        external = Database(tmp_path / "external.sqlite")
        external.set_setting("restore_marker", "imported")
        with sqlite3.connect(external.path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        imported = client.put("/api/backups/import?filename=external.sqlite", content=external.path.read_bytes(), headers={"Content-Type": "application/x-sqlite3"})
        assert imported.status_code == 200
        imported_record = imported.json()
        assert imported_record["record"]["trigger_type"] == "external_import"
        assert imported_record["preview"]["integrity"] == "ok"

        csrf = client.get("/api/backup/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        confirmation = client.post(f"/api/backups/{imported_record['record']['id']}/restore-confirmation", headers=headers)
        assert confirmation.status_code == 200
        restored = client.post(f"/api/backups/{imported_record['record']['id']}/restore", headers=headers, json=confirmation.json())
        assert restored.status_code == 200
        assert app.state.db.get_setting("restore_marker") == "imported"


def test_backup_scheduler_runs_once_at_startup() -> None:
    async def run() -> None:
        calls: list[str] = []

        async def job(trigger: str) -> None:
            calls.append(trigger)

        scheduler = BackupScheduler(job, lambda: {"backup_enabled": "true", "backup_schedule": "startup"})
        scheduler.start()
        await asyncio.sleep(0.02)
        await scheduler.stop()
        assert calls == ["startup"]

    asyncio.run(run())


def test_external_database_import_preview_and_restore(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.db")
    backups = BackupService(db, tmp_path / "backups")
    external = Database(tmp_path / "external.sqlite")
    external.set_setting("restore_marker", "external")
    with sqlite3.connect(external.path) as conn:
        conn.execute("CREATE TRIGGER external_trigger AFTER INSERT ON settings BEGIN DELETE FROM products; END")
    external_size = external.path.stat().st_size
    db.set_setting("restore_marker", "current")

    imported = backups.import_external(external.path, "source.sqlite", 10 * 1024 * 1024)
    assert imported["record"]["trigger_type"] == "external_import"
    assert imported["record"]["file_name"].startswith("import_")
    assert imported["preview"] == {
        "file_size": external_size,
        "product_count": 0,
        "snapshot_count": 0,
        "oldest_snapshot_at": None,
        "latest_snapshot_at": None,
        "integrity": "ok",
    }
    assert not external.path.exists()
    with sqlite3.connect(imported["record"]["file_path"]) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchone() is None

    backups.restore(imported["record"]["id"])
    assert db.get_setting("restore_marker") == "external"


def test_external_database_import_rejects_non_sqlite_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.db")
    backups = BackupService(db, tmp_path / "backups")
    invalid = tmp_path / "invalid.db"
    invalid.write_text("not a database")

    with pytest.raises(RuntimeError, match="SQLite"):
        backups.import_external(invalid, "invalid.db", 1024)
    assert not invalid.exists()
    assert not db.backup_records()


def test_external_database_import_rejects_incompatible_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.db")
    backups = BackupService(db, tmp_path / "backups")
    invalid = tmp_path / "invalid.sqlite"
    with sqlite3.connect(invalid) as conn:
        conn.executescript("""
            CREATE TABLE products (id TEXT, title TEXT);
            CREATE TABLE snapshots (product_id TEXT, captured_at TEXT, sold INTEGER);
            CREATE TABLE settings (key TEXT, value TEXT);
        """)

    with pytest.raises(RuntimeError, match="主键"):
        backups.import_external(invalid, "invalid.sqlite", 1024 * 1024)
    assert not invalid.exists()


def test_restore_rolls_back_when_post_replace_step_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "monitor.db")
    backups = BackupService(db, tmp_path / "backups")
    db.set_setting("restore_marker", "backup")
    record = backups.create("manual")
    db.set_setting("restore_marker", "current")

    def fail_reconcile() -> None:
        raise RuntimeError("simulated post-replace failure")

    monkeypatch.setattr(backups, "reconcile_records", fail_reconcile)
    with pytest.raises(RuntimeError, match="simulated"):
        backups.restore(record["id"])
    assert db.get_setting("restore_marker") == "current"
