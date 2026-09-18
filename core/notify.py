from __future__ import annotations

from collections.abc import Awaitable, Callable

CHANNEL_LABELS = {"wecom": "企业微信", "feishu": "飞书"}
Sender = Callable[[str, str], Awaitable[None]]


def mask_webhook(value: str | None) -> str | None:
    if not value: return None
    return value[:18] + "…" + value[-6:] if len(value) > 28 else "已配置"


def split_message(text: str, limit: int = 3900) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current); current = ""
        current += line
    return chunks + ([current] if current else [])


def is_masked(value: str | None) -> bool:
    return value == "已配置" or bool(value and "…" in value)


def test_message(channel: str) -> str:
    return f"✅ 红薯雷达{CHANNEL_LABELS[channel]}通知测试成功"


def notification_error(settings: dict[str, str | None]) -> str | None:
    """When notifications are enabled, the selected channel must have a configured webhook."""
    if settings.get("notify_enabled", settings.get("wecom_enabled")) != "true": return None
    channel = settings.get("notify_channel") or "wecom"
    if settings.get(f"{channel}_webhook"): return None
    other = "feishu" if channel == "wecom" else "wecom"
    return f"已启用通知，但{CHANNEL_LABELS[channel]} Webhook 未配置，请补填、切换到{CHANNEL_LABELS[other]}或关闭通知"


def resolve_sender(settings: dict[str, str | None]) -> tuple[str, str, Sender] | None:
    if settings.get("notify_enabled", settings.get("wecom_enabled")) != "true": return None
    channel = settings.get("notify_channel") or "wecom"
    if channel == "feishu":
        webhook = settings.get("feishu_webhook")
        if not webhook: return None
        from .feishu import send_text
        return "feishu", webhook, send_text
    webhook = settings.get("wecom_webhook")
    if not webhook: return None
    from .wecom import send_markdown
    return "wecom", webhook, send_markdown
