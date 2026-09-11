from __future__ import annotations

import httpx

from .notify import mask_webhook, split_message  # noqa: F401  (mask_webhook re-exported for compatibility)


def valid_webhook(value: str) -> bool:
    return value.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=") and len(value.rsplit("=", 1)[-1]) >= 8


async def send_markdown(webhook: str, text: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    if not valid_webhook(webhook): raise ValueError("企业微信 Webhook 格式无效")
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        for part in split_message(text):
            response = await client.post(webhook, json={"msgtype": "markdown", "markdown": {"content": part}})
            response.raise_for_status()
            if response.json().get("errcode", 0) != 0: raise RuntimeError(f"企业微信返回错误：{response.text[:300]}")
