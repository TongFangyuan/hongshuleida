from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class CollectionState(str, Enum):
    SUCCESS = "success"
    TEMPORARY_FAILURE = "temporary_failure"
    RESTRICTED = "restricted"
    DELISTED = "delisted"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProductData:
    id: str
    title: str
    shop_name: str | None
    shop_id: str | None
    cover: str | None
    sold: int
    shop_sold: int | None
    price: float | None
    fans: int | None
    stock_status: int | None
    deliverable: bool | None


@dataclass(frozen=True)
class CollectionResult:
    state: CollectionState
    product: ProductData | None = None
    method: str = "http"
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Snapshot:
    product_id: str
    captured_at: datetime
    sold: int
    shop_sold: int | None
    price: float | None
    fans: int | None
    stock_status: int | None
    deliverable: bool | None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
