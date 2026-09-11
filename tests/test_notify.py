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
