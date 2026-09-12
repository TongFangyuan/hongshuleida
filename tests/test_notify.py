import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from core.database import Database
from core import notify
from core.feishu import send_text, valid_webhook
from core.models import CollectionResult, CollectionState, ProductData, Snapshot
from core.notify import is_masked, mask_webhook, resolve_sender
from core.services import MonitoringService
from core.wecom import send_markdown

TZ = ZoneInfo("Asia/Shanghai")
FEISHU_HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"
WECOM_HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36


def transport(payloads: list[httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return payloads[len(requests) - 1]

    return httpx.MockTransport(handler), requests


def test_webhook_helpers() -> None:
    assert valid_webhook(FEISHU_HOOK) and not valid_webhook("https://open.feishu.cn/hook/short")
    assert mask_webhook(FEISHU_HOOK) == FEISHU_HOOK[:18] + "…" + FEISHU_HOOK[-6:]
    assert mask_webhook("short") == "已配置" and mask_webhook(None) is None
    assert is_masked(mask_webhook(FEISHU_HOOK)) and not is_masked(FEISHU_HOOK)
    assert notify.test_message("feishu") == "✅ 红薯雷达飞书通知测试成功"

def test_resolve_sender_channel_selection_and_fallbacks() -> None:
    assert resolve_sender({"wecom_enabled": "true", "wecom_webhook": WECOM_HOOK}) == ("wecom", WECOM_HOOK, send_markdown)
    assert resolve_sender({"notify_enabled": "true", "wecom_webhook": WECOM_HOOK})[0] == "wecom"
    assert resolve_sender({"notify_enabled": "true", "notify_channel": "feishu", "feishu_webhook": FEISHU_HOOK}) == ("feishu", FEISHU_HOOK, send_text)
    assert resolve_sender({"notify_enabled": "true", "notify_channel": "feishu"}) is None
    assert resolve_sender({"notify_enabled": "false", "wecom_enabled": "true", "wecom_webhook": WECOM_HOOK}) is None
    assert resolve_sender({"wecom_webhook": WECOM_HOOK}) is None


def test_feishu_send_text_formats_request_and_handles_errors() -> None:
    mock, requests = transport([httpx.Response(200, json={"code": 0, "msg": "success"}), httpx.Response(200, json={"StatusCode": 0, "StatusMessage": "success"})])
    asyncio.run(send_text(FEISHU_HOOK, "hello", transport=mock))
    asyncio.run(send_text(FEISHU_HOOK, "legacy", transport=mock))
    assert [str(r.url) for r in requests] == [FEISHU_HOOK, FEISHU_HOOK]
    assert [json.loads(r.content) for r in requests] == [{"msg_type": "text", "content": {"text": "hello"}}, {"msg_type": "text", "content": {"text": "legacy"}}]

    failing, _ = transport([httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})])
    with pytest.raises(RuntimeError): asyncio.run(send_text(FEISHU_HOOK, "x", transport=failing))
    with pytest.raises(ValueError): asyncio.run(send_text("https://example.com/webhook", "x"))


def test_wecom_send_markdown_still_works() -> None:
    mock, requests = transport([httpx.Response(200, json={"errcode": 0})])
    asyncio.run(send_markdown(WECOM_HOOK, "hello", transport=mock))
    assert json.loads(requests[0].content) == {"msgtype": "markdown", "markdown": {"content": "hello"}}


def test_notify_routes_to_selected_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "monitor.db")
    service = MonitoringService(db, None)
    service.confirm_products([{"id": "a" * 24, "title": "商品", "shop_name": "店铺", "shop_id": "shop1", "cover": None, "sold": 5, "shop_sold": None, "price": 9.9, "fans": None, "stock_status": None, "deliverable": None}])
    for key, value in (("notify_enabled", "true"), ("notify_channel", "feishu"), ("feishu_webhook", FEISHU_HOOK), ("wecom_shop_ids", "shop1")):
        db.set_setting(key, value)
    sent: list[tuple[str, str]] = []

    async def fake_send(webhook: str, text: str) -> None:
        sent.append((webhook, text))

    monkeypatch.setattr("core.feishu.send_text", fake_send)
    asyncio.run(service._notify_if_enabled(datetime.now(TZ), []))
    assert sent and sent[0][0] == FEISHU_HOOK and "时报" in sent[0][1]
    monkeypatch.setattr("core.wecom.send_markdown", fake_send)
    db.set_setting("notify_channel", "wecom"); db.set_setting("wecom_webhook", WECOM_HOOK)
    asyncio.run(service._notify_if_enabled(datetime.now(TZ), []))
    assert len(sent) == 2 and sent[1][0] == WECOM_HOOK
    assert service.settings_public()["feishu_webhook"] == mask_webhook(FEISHU_HOOK)


def test_feishu_report_is_sorted_for_the_previous_complete_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "monitor.db")
    service = MonitoringService(db, None)
    products = [
        {"id": "a" * 24, "title": "甲商品", "shop_name": "甲店", "shop_id": "shop-a", "cover": None, "sold": 10, "shop_sold": None, "price": 9.9, "fans": None, "stock_status": None, "deliverable": None},
        {"id": "b" * 24, "title": "乙商品", "shop_name": "乙店", "shop_id": "shop-b", "cover": None, "sold": 20, "shop_sold": None, "price": 19.9, "fans": None, "stock_status": None, "deliverable": None},
    ]
    service.confirm_products(products)
    report_boundary = datetime(2026, 9, 6, 13, tzinfo=TZ)
    for product, baseline, finish in zip(products, (10, 20), (14, 29)):
        db.save_snapshot(Snapshot(product["id"], report_boundary.replace(hour=12), baseline, None, product["price"], None, None, None))
        db.save_snapshot(Snapshot(product["id"], report_boundary, finish, None, product["price"], None, None, None))
    for key, value in (("notify_enabled", "true"), ("notify_channel", "feishu"), ("feishu_webhook", FEISHU_HOOK), ("wecom_shop_ids", "shop-a,shop-b")):
        db.set_setting(key, value)
    sent: list[str] = []

    async def fake_send(_: str, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("core.feishu.send_text", fake_send)
    asyncio.run(service._notify_if_enabled(report_boundary, []))

    assert len(sent) == 1
    assert "📊 12点店铺销量时报\n09月06日 12:00 - 12:59" in sent[0]
    assert sent[0].index("乙店") < sent[0].index("甲店")
    assert "上小时销量：9 单" in sent[0] and "上小时销量：4 单" in sent[0]


def test_no_selected_shops_is_logged_without_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "monitor.db")
    service = MonitoringService(db, None)
    db.set_setting("notify_enabled", "true")
    db.set_setting("notify_channel", "feishu")
    db.set_setting("feishu_webhook", FEISHU_HOOK)

    async def unexpected_send(_: str, __: str) -> None:
        raise AssertionError("通知店铺为空时不应发送")

    monkeypatch.setattr("core.feishu.send_text", unexpected_send)
    asyncio.run(service._notify_if_enabled(datetime(2026, 9, 6, 13, tzinfo=TZ), []))

    assert any("未设置通知店铺" in row["message"] for row in db.logs(source="feishu"))


def test_manual_collection_does_not_send_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Collector:
        async def collect(self, product_id: str) -> CollectionResult:
            return CollectionResult(CollectionState.SUCCESS, ProductData(product_id, "商品", "店铺", "shop1", None, 6, None, 9.9, None, None, None))

        async def pause(self) -> None:
            return None

    db = Database(tmp_path / "monitor.db")
    service = MonitoringService(db, Collector())
    service.confirm_products([{"id": "a" * 24, "title": "商品", "shop_name": "店铺", "shop_id": "shop1", "cover": None, "sold": 5, "shop_sold": None, "price": 9.9, "fans": None, "stock_status": None, "deliverable": None}])
    calls: list[datetime] = []

    async def record(boundary: datetime, _: list[dict]) -> None:
        calls.append(boundary)

    monkeypatch.setattr(service, "_notify_if_enabled", record)
    asyncio.run(service.collect_products())
    assert calls == []
    formal_time = datetime(2026, 9, 6, 13, tzinfo=TZ)
    asyncio.run(service.collect_products(formal_time=formal_time))
    assert calls == [formal_time]


def test_notification_shops_api_persists_selection_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RED_POTATO_RADAR_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from server.main import app

    product = {"id": "a" * 24, "title": "商品", "shop_name": "店铺", "shop_id": "shop1", "cover": None, "sold": 5, "shop_sold": None, "price": 9.9, "fans": None, "stock_status": None, "deliverable": None}
    with TestClient(app) as client:
        app.state.service.confirm_products([product])
        assert client.get("/api/notification-shops").json()["selected_shop_ids"] == []
        assert client.put("/api/notification-shops", json={"shop_ids": ["shop1"]}).json()["selected_shop_ids"] == ["shop1"]
        # The generic settings form must not overwrite a selection saved by its dedicated control.
        assert client.put("/api/settings", json={"values": {"wecom_shop_ids": ""}}).status_code == 200
        assert app.state.db.get_setting("wecom_shop_ids") == "shop1"


def test_settings_api_validation_masking_and_test_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RED_POTATO_RADAR_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from server.main import SettingsRequest, app

    with pytest.raises(ValidationError): SettingsRequest(values={"feishu_webhook": "https://example.com/hook/x"})
    with pytest.raises(ValidationError): SettingsRequest(values={"notify_channel": "dingtalk"})
    sent: list[tuple[str, str]] = []

    async def fake_send(webhook: str, text: str) -> None:
        sent.append((webhook, text))

    monkeypatch.setattr("core.feishu.send_text", fake_send)
    with TestClient(app) as client:
        assert client.post("/api/settings/test-notify").status_code == 400
        values = {"notify_channel": "feishu", "notify_enabled": "true", "feishu_webhook": FEISHU_HOOK}
        assert client.put("/api/settings", json={"values": values}).status_code == 200
        masked = client.get("/api/settings").json()["feishu_webhook"]
        assert masked != FEISHU_HOOK and is_masked(masked)
        assert client.put("/api/settings", json={"values": {"feishu_webhook": masked}}).status_code == 200
        assert app.state.db.get_setting("feishu_webhook") == FEISHU_HOOK
        assert client.put("/api/settings", json={"values": {"feishu_webhook": "https://example.com/hook/x"}}).status_code == 422
        response = client.post("/api/settings/test-notify")
        assert response.status_code == 200 and sent == [(FEISHU_HOOK, notify.test_message("feishu"))]


def test_notification_error_rules() -> None:
    assert notify.notification_error({}) is None
    assert notify.notification_error({"wecom_webhook": WECOM_HOOK}) is None
    assert notify.notification_error({"notify_enabled": "true", "wecom_webhook": WECOM_HOOK}) is None
    assert notify.notification_error({"notify_enabled": "true", "notify_channel": "feishu", "feishu_webhook": FEISHU_HOOK}) is None
    assert "企业微信" in notify.notification_error({"notify_enabled": "true"})
    assert "飞书" in notify.notification_error({"notify_enabled": "true", "notify_channel": "feishu"})
    assert notify.notification_error({"notify_enabled": "false", "wecom_enabled": "true"}) is None


def test_settings_api_null_values_and_channel_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RED_POTATO_RADAR_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from server.main import app

    with TestClient(app) as client:
        # The exact browser payload that used to 422: unconfigured channel sends null.
        values = {"wecom_webhook": None, "feishu_webhook": FEISHU_HOOK, "notify_channel": "feishu", "notify_enabled": "true"}
        response = client.put("/api/settings", json={"values": values})
        assert response.status_code == 200
        assert app.state.db.get_setting("feishu_webhook") == FEISHU_HOOK
        assert app.state.db.get_setting("wecom_webhook") is None
        assert is_masked(response.json()["feishu_webhook"])

        # Enabled but the selected channel has no webhook configured.
        assert client.put("/api/settings", json={"values": {"notify_channel": "wecom"}}).status_code == 422
        # Clearing the selected channel's webhook while enabled is rejected.
        assert client.put("/api/settings", json={"values": {"feishu_webhook": ""}}).status_code == 422
        # Clearing the *other* channel is fine, and switching to the configured one is fine.
        assert client.put("/api/settings", json={"values": {"wecom_webhook": ""}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"notify_channel": "wecom", "wecom_webhook": WECOM_HOOK}}).status_code == 200
        # Disabled notifications never require a webhook.
        assert client.put("/api/settings", json={"values": {"notify_enabled": "false", "notify_channel": "feishu", "feishu_webhook": "", "wecom_webhook": ""}}).status_code == 200
        # Null numeric fields are skipped instead of crashing the validator.
        assert client.put("/api/settings", json={"values": {"interval_min_seconds": None, "notify_channel": "wecom", "wecom_webhook": WECOM_HOOK, "notify_enabled": "true"}}).status_code == 200


def test_settings_api_controls_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("RED_POTATO_RADAR_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from server.main import app

    with TestClient(app) as client:
        scheduler = app.state.scheduler
        assert not scheduler.running
        status = client.get("/api/collect/status").json()
        assert status["scheduled"] is False and status["period_minutes"] == 60 and status["next_run_at"] is None

        assert client.put("/api/settings", json={"values": {"monitoring_enabled": "true", "collection_period_minutes": "30"}}).status_code == 200
        assert scheduler.running and scheduler.period_minutes == 30
        for _ in range(50):
            status = client.get("/api/collect/status").json()
            if status["next_run_at"]: break
            time.sleep(0.01)
        assert status["scheduled"] is True and status["period_minutes"] == 30 and status["next_run_at"]

        assert client.put("/api/settings", json={"values": {"collection_period_minutes": "15"}}).status_code == 200
        assert scheduler.running and scheduler.period_minutes == 15

        assert client.put("/api/settings", json={"values": {"monitoring_enabled": "false"}}).status_code == 200
        assert not scheduler.running
        assert client.get("/api/collect/status").json()["scheduled"] is False
