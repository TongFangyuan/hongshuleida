import asyncio

import httpx

from core.collector import ProductCollector, extract_product_id, extract_share_link, parse_count, parse_product
from core.models import CollectionState


PRODUCT_ID = "a" * 24


def payload() -> dict:
    return {"success": True, "data": {"template_data": [{
        "descriptionMain": {"name": "商品"}, "sellerH5": {"name": "店铺", "id": "s1", "salesVolume": "1.1万"},
        "carouselH5": {"images": [{"url": "//image.example/a.jpg"}], "stockStatus": 1},
        "priceH5": {"itemAnalysisDataText": "已售1.2w", "dealPrice": {"price": "19.90"}},
        "bottomBarMainH5": {"deliveryInfo": {"ableToDelivery": True}}, "profitBarPopupH5": {"follow": {"fansNum": "2万"}}
    }]}}


def test_input_and_xhs_payload_parser() -> None:
    assert extract_product_id(f"分享 https://www.xiaohongshu.com/goods-detail/{PRODUCT_ID}?x=1") == PRODUCT_ID
    assert extract_share_link("分享 https://xhslink.com/m/AbC123，打开小红书查看") == "https://xhslink.com/m/AbC123"
    assert parse_count("1.2万") == 12000
    result = parse_product(payload(), PRODUCT_ID)
    assert result.state is CollectionState.SUCCESS
    assert result.product and result.product.sold == 12000 and result.product.cover == "https://image.example/a.jpg"


def test_explicit_delisted_is_not_temporary_failure() -> None:
    result = parse_product({"success": False, "message": "当前商品已下架"}, PRODUCT_ID)
    assert result.state is CollectionState.DELISTED


def test_resolve_fetches_link_extracted_from_share_text() -> None:
    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get(self, url: str) -> httpx.Response:
            self.urls.append(url)
            return httpx.Response(200, request=httpx.Request("GET", f"https://www.xiaohongshu.com/goods-detail/{PRODUCT_ID}"))

    collector = object.__new__(ProductCollector)
    client = Client()
    collector.client = client

    result = asyncio.run(collector.resolve("【小红书】商品分享 https://xhslink.com/m/AbC123 点击链接查看"))

    assert result == PRODUCT_ID
    assert client.urls == ["https://xhslink.com/m/AbC123"]
