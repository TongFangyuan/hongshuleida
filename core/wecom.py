from __future__ import annotations

from typing import Iterable

import httpx


def mask_webhook(value: str | None) -> str | None:
    if not value: return None
    return value[:18] + "…" + value[-6:] if len(value) > 28 else "已配置"


def valid_webhook(value: str) -> bool:
    return value.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=") and len(value.rsplit("=", 1)[-1]) >= 8


def split_message(text: str, limit: int = 3900) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current); current = ""
        current += line
    return chunks + ([current] if current else [])


async def send_markdown(webhook: str, text: str) -> None:
    if not valid_webhook(webhook): raise ValueError("企业微信 Webhook 格式无效")
    async with httpx.AsyncClient(timeout=15) as client:
        for part in split_message(text):
            response = await client.post(webhook, json={"msgtype": "markdown", "markdown": {"content": part}})
            response.raise_for_status()
            if response.json().get("errcode", 0) != 0: raise RuntimeError(f"企业微信返回错误：{response.text[:300]}")
