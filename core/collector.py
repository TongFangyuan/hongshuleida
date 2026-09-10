from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import SHANGHAI_TZ
from .models import CollectionResult, CollectionState, ProductData, as_dict

DETAIL_ENDPOINT = "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc?version=0.0.5&item_id={item_id}"
DETAIL_PAGE = "https://www.xiaohongshu.com/goods-detail/{item_id}"
PRODUCT_ID_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
XHS_LINK_RE = re.compile(r"https?://xhslink\.com/[A-Za-z0-9/_-]+", re.IGNORECASE)


class CollectionError(Exception):
    pass


def extract_product_id(value: str) -> str | None:
    """Accept shared text, a final URL, or a standalone 24-character ID."""
    match = PRODUCT_ID_RE.search(value)
    return match.group(0).lower() if match else None


def extract_share_link(value: str) -> str | None:
    """Extract an xhslink short URL from pasted share text."""
    match = XHS_LINK_RE.search(value)
    return match.group(0) if match else None


def parse_count(value: Any) -> int | None:
    if value is None: return None
    if isinstance(value, (int, float)): return int(value)
    text = str(value).lower().replace(",", "").replace("已售", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|w)?", text)
    if not match: return None
    multiplier = 10000 if match.group(2) else 1
    return int(float(match.group(1)) * multiplier)


def parse_price(value: Any) -> float | None:
    if value is None: return None
    if isinstance(value, dict): value = value.get("price") or value.get("value")
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def find_template(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = as_dict(payload.get("data"))
    templates = data.get("template_data") or data.get("templateData")
    return as_dict(templates[0]) if isinstance(templates, list) and templates and isinstance(templates[0], dict) else None


def delist_reason(payload: Any) -> str | None:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    if any(marker in text for marker in ("当前商品违规，无法展示", "item freeze", "违规下架")):
        return "违规下架"
    if any(marker in text for marker in ("unBuyableGoShop", "已下架，进店逛逛", "当前商品已下架", "商品不存在", "商品已失效")):
        return "商品已下架"
    return None


def parse_product(payload: dict[str, Any], item_id: str) -> CollectionResult:
    if delisted := delist_reason(payload):
        return CollectionResult(CollectionState.DELISTED, reason=delisted)
    if payload.get("success") is not True:
        return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="接口未确认成功", detail=str(payload)[:500])
    template = find_template(payload)
    if not template:
        return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="商品数据不完整")
    main, h5 = as_dict(template.get("descriptionMain")), as_dict(template.get("descriptionH5"))
    seller, price, bottom = as_dict(template.get("sellerH5")), as_dict(template.get("priceH5")), as_dict(template.get("bottomBarMainH5"))
    carousel = as_dict(template.get("carouselH5"))
    title = main.get("name") or h5.get("name")
    # An absent sales field is a malformed capture, while an explicitly empty field under success is real zero.
    if not title or "itemAnalysisDataText" not in price:
        return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="关键字段缺失")
    sold = parse_count(price.get("itemAnalysisDataText"))
    if sold is None: sold = 0 if not str(price.get("itemAnalysisDataText") or "").strip() else None
    if sold is None: return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="累计销量格式无法识别")
    images = carousel.get("images") if isinstance(carousel.get("images"), list) else []
    cover = as_dict(images[0]).get("url") if images else None
    if cover and str(cover).startswith("//"): cover = "https:" + str(cover)
    follow = as_dict(as_dict(template.get("profitBarPopupH5")).get("follow"))
    actual_price = parse_price(as_dict(price.get("dealPrice")).get("price"))
    actual_price = actual_price if actual_price is not None else parse_price(as_dict(bottom.get("dealPrice")).get("price"))
    actual_price = actual_price if actual_price is not None else parse_price(price.get("highlightPrice"))
    delivery = as_dict(bottom.get("deliveryInfo")).get("ableToDelivery")
    return CollectionResult(CollectionState.SUCCESS, ProductData(
        id=item_id, title=str(title), shop_name=seller.get("name"), shop_id=str(seller["id"]) if seller.get("id") is not None else None,
        cover=cover, sold=sold, shop_sold=parse_count(seller.get("salesVolume")), price=actual_price,
        fans=parse_count(follow.get("fansNum") or seller.get("fansAmount")), stock_status=carousel.get("stockStatus"),
        deliverable=bool(delivery) if delivery is not None else None,
    ))


class ProductCollector:
    """Three-stage per-item collector. Browser use is deliberately lazy and optional."""
    def __init__(self, profile_dir: Path, min_delay: float = 0.1, max_delay: float = 0.3, proxy: str | None = None):
        self.profile_dir, self.min_delay, self.max_delay, self.proxy = profile_dir, min_delay, max_delay, proxy
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=True, proxy=proxy, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._browser = self._context = None

    async def close(self) -> None:
        if self._context: await self._context.close()
        if self._browser: await self._browser.close()
        if getattr(self, "_playwright", None): await self._playwright.stop()
        await self.client.aclose()

    async def resolve(self, text: str) -> str | None:
        direct = extract_product_id(text)
        if direct: return direct
        link = extract_share_link(text)
        if not link: return None
        try:
            response = await self.client.get(link)
            return extract_product_id(str(response.url)) or extract_product_id(response.text)
        except (httpx.HTTPError, ValueError):
            return None

    async def collect(self, item_id: str) -> CollectionResult:
        result = await self._http(item_id)
        if result.state in (CollectionState.SUCCESS, CollectionState.DELISTED): return result
        browser_result = await self._browser_collect(item_id, visible=False)
        if browser_result.state in (CollectionState.SUCCESS, CollectionState.DELISTED): return browser_result
        return await self._browser_collect(item_id, visible=True)

    async def _http(self, item_id: str) -> CollectionResult:
        try:
            response = await self.client.get(DETAIL_ENDPOINT.format(item_id=item_id))
            if response.status_code == 461:
                return CollectionResult(CollectionState.RESTRICTED, reason="HTTP 461：请求环境受限")
            response.raise_for_status()
            return parse_product(response.json(), item_id)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="轻量接口请求失败", detail=repr(exc))

    async def _ensure_context(self) -> Any:
        if self._context: return self._context
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._context = await self._launch_persistent(self._playwright, self.profile_dir, headless=True)
            return self._context
        except Exception as exc:
            return exc

    async def _browser_collect(self, item_id: str, visible: bool) -> CollectionResult:
        temporary: Path | None = None
        context = None
        pw = None
        try:
            if visible:
                from playwright.async_api import async_playwright
                temporary = Path(tempfile.mkdtemp(prefix="red-potato-radar-"))
                pw = await async_playwright().start()
                # A fresh persistent profile prevents a restricted long-lived profile from poisoning this fallback.
                context = await self._launch_persistent(pw, temporary, headless=False)
            else:
                context = await self._ensure_context()
                if isinstance(context, Exception):
                    return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="未找到可用浏览器", detail=repr(context))
            page = await context.new_page()
            response = await page.goto(DETAIL_ENDPOINT.format(item_id=item_id), wait_until="domcontentloaded", timeout=30000)
            body = await response.text() if response else ""
            try: result = parse_product(json.loads(body), item_id)
            except json.JSONDecodeError:
                await page.goto(DETAIL_PAGE.format(item_id=item_id), wait_until="domcontentloaded", timeout=30000)
                body = await page.evaluate("""async (url) => (await fetch(url, {credentials: 'include'})).text()""", DETAIL_ENDPOINT.format(item_id=item_id))
                result = parse_product(json.loads(body), item_id)
            await page.close()
            return CollectionResult(result.state, result.product, "visible_browser" if visible else "headless_browser", result.reason, result.detail)
        except Exception as exc:
            return CollectionResult(CollectionState.TEMPORARY_FAILURE, reason="可见浏览器采集失败" if visible else "静默浏览器采集失败", detail=repr(exc))
        finally:
            if visible:
                if context: await context.close()
                if pw: await pw.stop()
                if temporary: shutil.rmtree(temporary, ignore_errors=True)

    async def pause(self) -> None:
        await asyncio.sleep(random.uniform(max(0.1, self.min_delay), min(60.0, max(self.min_delay, self.max_delay))))

    async def _launch_persistent(self, playwright: Any, profile: Path, headless: bool) -> Any:
        """Prefer installed system browsers, then Playwright Chromium, per platform."""
        channels = ["chrome", "msedge", None] if sys.platform == "win32" else ["chrome", None]
        failures = []
        for channel in channels:
            try:
                options = {"headless": headless, "locale": "zh-CN", "timezone_id": SHANGHAI_TZ, "viewport": {"width": 1440, "height": 900}}
                if channel: options["channel"] = channel
                return await playwright.chromium.launch_persistent_context(str(profile), **options)
            except Exception as exc:
                failures.append(f"{channel or 'playwright-chromium'}: {exc}")
        raise CollectionError("找不到可用 Chrome/Edge/Playwright Chromium：" + " | ".join(failures))
