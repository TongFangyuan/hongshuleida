from __future__ import annotations

import httpx

from .notify import split_message


def valid_webhook(value: str) -> bool:
    return value.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/") and len(value.rsplit("/", 1)[-1]) >= 8


async def send_text(webhook: str, text: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    if not valid_webhook(webhook): raise ValueError("飞书 Webhook 格式无效")
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        for part in split_message(text):
            response = await client.post(webhook, json={"msg_type": "text", "content": {"text": part}})
            response.raise_for_status()
            payload = response.json()
            if payload.get("code", payload.get("StatusCode", 0)) != 0: raise RuntimeError(f"飞书返回错误：{response.text[:300]}")
